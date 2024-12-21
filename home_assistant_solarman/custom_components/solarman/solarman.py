import asyncio
import logging
import threading
import typing as t
from datetime import datetime

import yaml
from homeassistant.util import Throttle
from pysolarmanv5 import PySolarmanV5

from . import const, parser

log = logging.getLogger(__name__)

QUERY_RETRY_ATTEMPTS = 2


class Inverter:
    def __init__(self, path, serial, host, port, mb_slaveid, lookup_file):
        self._modbus = None
        self._serial = serial
        self.path = path
        self._host = host
        self._port = port
        self._mb_slaveid = mb_slaveid
        self._current_val = None
        self._status_connection = -1
        self.status_lastUpdate = "N/A"
        self.lookup_file = lookup_file
        self.lock = threading.Lock()
        self._parameter_definition = None

        if not self.lookup_file or lookup_file == "parameters.yaml":
            self.lookup_file = "deye_hybrid.yaml"

    async def read_parameter_definition(self) -> dict[str, t.Any]:
        def blocking_read_parameter_definition(filename: str) -> None:
            log.debug("Reading solarman parameters from %s", filename)
            with open(filename) as f:
                self._parameter_definition = yaml.full_load(f)

        if self._parameter_definition is None:
            await asyncio.get_running_loop().run_in_executor(
                None,
                blocking_read_parameter_definition,
                self.path + self.lookup_file,
            )

        return self._parameter_definition

    @property
    def status_connection(self):
        return "Connected" if self._status_connection == 1 else "Disconnected"

    def is_connected_to_server(self):
        return self._modbus is not None

    def connect_to_server(self):
        if self._modbus:
            return self._modbus
        log.info(
            f"Connecting to solarman data logger {self._host}:{self._port}"
        )
        self._modbus = PySolarmanV5(
            self._host,
            self._serial,
            port=self._port,
            mb_slave_id=self._mb_slaveid,
            logger=None,
            auto_reconnect=True,
            socket_timeout=15,
        )

    def disconnect_from_server(self):
        if self._modbus:
            try:
                log.info(
                    "Disconnecting from solarman data logger "
                    f"{self._host}:{self._port}"
                )
                self._modbus.disconnect()
            finally:
                self._modbus = None

    def send_request(self, params, start, end, mb_fc):
        length = end - start + 1
        match mb_fc:
            case 3:
                response = self._modbus.read_holding_registers(
                    register_addr=start, quantity=length
                )
            case 4:
                response = self._modbus.read_input_registers(
                    register_addr=start, quantity=length
                )
        params.parse(response, start, length)

    @Throttle(const.MIN_TIME_BETWEEN_UPDATES)
    async def update(self):
        await self.get_statistics()
        return

    async def get_statistics(self):
        result = 1
        parameters = await self.read_parameter_definition()
        params = parser.ParameterParser(parameters)
        requests = parameters["requests"]
        log.debug(f"Starting to query for [{len(requests)}] ranges...")

        with self.lock:
            try:
                isConnected = self._status_connection == 1
                for request in requests:
                    start = request["start"]
                    end = request["end"]
                    mb_fc = request["mb_functioncode"]
                    log.debug(f"Querying [{start} - {end}]...")

                    attempts_left = QUERY_RETRY_ATTEMPTS
                    while attempts_left > 0:
                        attempts_left -= 1
                        try:
                            self.connect_to_server()
                            self.send_request(params, start, end, mb_fc)
                            result = 1
                        except Exception as e:
                            result = 0
                            log.log(
                                (
                                    logging.WARNING
                                    if isConnected
                                    else logging.DEBUG
                                ),
                                f"Querying [{start} - {end}] failed with "
                                f"exception [{type(e).__name__}: {e}]",
                            )
                            self.disconnect_from_server()
                        if result == 0:
                            log.log(
                                (
                                    logging.WARNING
                                    if isConnected
                                    else logging.DEBUG
                                ),
                                f"Querying [{start} - {end}] failed, "
                                f"[{attempts_left}] retry attempts left",
                            )
                        else:
                            log.debug(f"Querying [{start} - {end}] succeeded")
                            break
                    if result == 0:
                        log.log(
                            (
                                logging.WARNING
                                if isConnected
                                else logging.DEBUG
                            ),
                            f"Querying registers [{start} - {end}] failed, "
                            "aborting.",
                        )
                        break

                if result == 1:
                    log.debug(
                        "All queries succeeded, exposing updated values."
                    )
                    self.status_lastUpdate = datetime.now().strftime(
                        "%m/%d/%Y, %H:%M:%S"
                    )
                    self._status_connection = 1
                    self._current_val = params.get_result()
                else:
                    self._status_connection = 0
                    # Clear cached previous results to not report stale
                    # and incorrect data
                    self._current_val = {}
                    self.disconnect_from_server()
            except Exception as e:
                log.warning(
                    f"Querying inverter {self._serial} at "
                    f"{self._host}:{self._port} failed on connection start "
                    f"with exception [{type(e).__name__}: {e}]"
                )
                self._status_connection = 0
                # Clear cached previous results to not report stale
                # and incorrect data
                self._current_val = {}
                self.disconnect_from_server()

    def get_current_val(self):
        return self._current_val

    async def get_sensors(self):
        parameters = await self.read_parameter_definition()
        params = parser.ParameterParser(parameters)
        for param in params.get_sensors():
            yield param

    # Service calls
    def service_read_holding_register(self, register):
        log.debug(f"Service Call: read_holding_register : [{register}]")

        with self.lock:
            try:
                wasConnected = self.is_connected_to_server()
                self.connect_to_server()
                response = self._modbus.read_holding_registers(register, 1)
                log.info(
                    f"Service Call: read_holding_registers : [{register}] "
                    f"value [{response}]"
                )
                if not wasConnected:
                    self.disconnect_from_server()
            except Exception as e:
                log.warning(
                    f"Service Call: read_holding_registers : [{register}] "
                    f"failed with exception [{type(e).__name__}: {e}]"
                )
                self.disconnect_from_server()
                raise e

        return response

    def service_read_multiple_holding_registers(self, register, count):
        log.debug(
            f"Service Call: read_holding_register : [{register}], "
            f"count : {count}"
        )

        with self.lock:
            try:
                wasConnected = self.is_connected_to_server()
                self.connect_to_server()
                response = self._modbus.read_holding_registers(register, count)
                log.info(
                    f"Service Call: read_holding_registers : [{register}] "
                    f"value [{response}]"
                )
                if not wasConnected:
                    self.disconnect_from_server()
            except Exception as e:
                log.warning(
                    f"Service Call: read_holding_registers : [{register}] "
                    f"failed with exception [{type(e).__name__}: {e}]"
                )
                self.disconnect_from_server()
                raise e

        return response

    def service_write_holding_register(self, register, value):
        log.debug(
            f"Service Call: write_holding_register : [{register}], "
            f"value : [{value}]"
        )
        with self.lock:
            try:
                wasConnected = self.is_connected_to_server()
                self.connect_to_server()
                self._modbus.write_holding_register(register, value)
                if not wasConnected:
                    self.disconnect_from_server()
            except Exception as e:
                log.warning(
                    f"Service Call: write_holding_register : [{register}], "
                    f"value : [{value}] failed with exception "
                    f"[{type(e).__name__}: {e}]"
                )
                self.disconnect_from_server()
                raise e
        return

    def service_write_multiple_holding_registers(self, register, values):
        log.debug(
            "Service Call: write_multiple_holding_registers: "
            f"[{register}], values : [{values}]"
        )
        with self.lock:
            try:
                wasConnected = self.is_connected_to_server()
                self.connect_to_server()
                self._modbus.write_multiple_holding_registers(register, values)
                if not wasConnected:
                    self.disconnect_from_server()
            except Exception as e:
                log.warning(
                    "Service Call: write_multiple_holding_registers: "
                    f"[{register}], values : [{values}] failed with exception "
                    f"[{type(e).__name__}: {e}]"
                )
                self.disconnect_from_server()
                raise e
        return
