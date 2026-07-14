"""Yoctopuce thermocouple I/O (Yocto-Thermocouple, two inputs per module).

Connects directly over USB by default (port ``usb``), or to a VirtualHub /
YoctoHub when the port looks like ``host[:port]``. Direct USB needs exclusive
access — quit VirtualHub if it has claimed the module.

Readings are polled with ``get_currentValue()`` from the caller's sampling
loop; the timed-report callback machinery of the Yoctopuce API is not used.
The yoctopuce package bundles its native libraries for Linux/macOS/Windows,
so nothing here is platform-specific.
"""

from __future__ import annotations

import sys
import time

from .spc_io import Reading

TEMP_FUNCTIONS = ("temperature1", "temperature2")
# While a sensor is offline, re-scan the device list at most this often so a
# module plugged in (or back in) mid-session comes online without a restart.
_DEVLIST_MIN_S = 2.0
# YAPI is process-global; free it once no matter how many channels close.
_api_freed = False


def _yocto():
    """Import the yoctopuce API lazily so the package is optional at import time."""
    try:
        from yoctopuce.yocto_api import YAPI, YAPI_Exception, YRefParam
        from yoctopuce.yocto_temperature import YTemperature
    except ImportError:
        raise SystemExit(
            "yoctopuce package not found (needed for --mode temperature). "
            "Run:  pip install yoctopuce"
        )
    return YAPI, YAPI_Exception, YRefParam, YTemperature


def hub_target(port: str | None) -> str:
    """Map a --port value to a Yoctopuce hub URL: 'usb' or a host[:port]."""
    if not port or port == "usb":
        return "usb"
    return port


def register_hub(target: str) -> None:
    """RegisterHub with retries while a starting VirtualHub reports 'not ready'."""
    YAPI, YAPI_Exception, YRefParam, _ = _yocto()
    errmsg = YRefParam()
    for attempt in range(10):
        try:
            if YAPI.RegisterHub(target, errmsg) == YAPI.SUCCESS:
                return
        except YAPI_Exception as exc:
            errmsg.value = str(exc)
        if "not ready" not in errmsg.value:
            break
        if attempt == 0:
            print("  Yoctopuce hub is initializing, waiting...", file=sys.stderr)
        time.sleep(1.0)
    hint = (
        " (direct USB needs exclusive access — quit VirtualHub if it is running)"
        if target == "usb" else ""
    )
    raise OSError(f"Cannot connect to Yoctopuce hub {target!r}: {errmsg.value}{hint}")


def discover_temperature_modules() -> list[str]:
    """Serial numbers of modules exposing temperature functions, in enum order."""
    YAPI, YAPI_Exception, YRefParam, YTemperature = _yocto()
    YAPI.UpdateDeviceList(YRefParam())
    serials: list[str] = []
    sensor = YTemperature.FirstTemperature()
    while sensor is not None:
        try:
            serial = sensor.get_module().get_serialNumber()
        except YAPI_Exception:
            serial = None
        if serial and serial not in serials:
            serials.append(serial)
        sensor = sensor.nextTemperature()
    return serials


class TemperatureChannel:
    """Both thermocouple inputs of one module, read together each poll.

    Opened on the main thread, then read exclusively from the sampler thread;
    close() runs after the sampler joins, so access is single-threaded at any
    instant.
    """

    def __init__(self, port: str, serial: str):
        YAPI, YAPI_Exception, YRefParam, YTemperature = _yocto()
        self._YAPI = YAPI
        self._YAPI_Exception = YAPI_Exception
        self._YRefParam = YRefParam
        self.port = port
        self.serial = serial
        self._sensors = tuple(
            YTemperature.FindTemperature(f"{serial}.{fn}") for fn in TEMP_FUNCTIONS
        )
        self._last_devlist = time.monotonic()

    def read(self) -> tuple[Reading | None, Reading | None]:
        """One Reading (°C) per thermocouple input; None while that input is offline."""
        out = []
        for sensor in self._sensors:
            reading = None
            try:
                if sensor.isOnline():
                    reading = Reading(sensor.get_currentValue(), "C")
            except self._YAPI_Exception:
                reading = None
            out.append(reading)
        if None in out:
            self._refresh_devices()
        return (out[0], out[1])

    def _refresh_devices(self) -> None:
        now = time.monotonic()
        if now - self._last_devlist < _DEVLIST_MIN_S:
            return
        self._last_devlist = now
        try:
            self._YAPI.UpdateDeviceList(self._YRefParam())
        except self._YAPI_Exception:
            pass

    def close(self) -> None:
        global _api_freed
        if _api_freed:
            return
        _api_freed = True
        try:
            self._YAPI.FreeAPI()
        except self._YAPI_Exception:
            pass


def open_temperature_channels(
    port: str = "usb", serials: list[str] | None = None
) -> list[TemperatureChannel]:
    """Connect to the hub and bind every module's thermocouple inputs.

    With pinned ``serials``, exactly those modules are bound in that order,
    even if currently offline (readings start when each appears). Otherwise
    all discovered modules are bound; at least one must be online.
    """
    target = hub_target(port)
    register_hub(target)
    found = discover_temperature_modules()
    if serials:
        for serial in serials:
            if serial not in found:
                print(
                    f"  {serial} is not online yet — readings start when it appears.",
                    file=sys.stderr,
                )
        return [TemperatureChannel(target, serial) for serial in serials]
    if not found:
        raise OSError(
            f"No Yocto-Thermocouple found on {target!r}.\n"
            "Plug in the module (direct USB needs exclusive access — quit "
            "VirtualHub if it is running), pass --port temperature=HOST:PORT "
            "for a hub, or pin --temp-serial to start before plugging in."
        )
    return [TemperatureChannel(target, serial) for serial in found]


def to_c(reading: Reading | None) -> float:
    return float("nan") if reading is None else reading.value


def format_temperature_device_list() -> str:
    lines = ["Yoctopuce thermocouple modules:"]
    try:
        from yoctopuce.yocto_api import YAPI_Exception
        from yoctopuce.yocto_temperature import YTemperature
    except ImportError:
        lines.append("  (yoctopuce package not installed — pip install yoctopuce)")
        return "\n".join(lines)
    try:
        register_hub("usb")
    except OSError as exc:
        lines.append(f"  ({exc})")
        return "\n".join(lines)
    serials = discover_temperature_modules()
    if not serials:
        lines.append(
            "  (none found — plug in Yocto-Thermocouple; if VirtualHub is "
            "running it has claimed the module)"
        )
    else:
        for sn in serials:
            states = []
            for fn in TEMP_FUNCTIONS:
                sensor = YTemperature.FindTemperature(f"{sn}.{fn}")
                try:
                    states.append(f"{fn} {'online' if sensor.isOnline() else 'offline'}")
                except YAPI_Exception:
                    states.append(f"{fn} ?")
            lines.append(f"  {sn}  ({', '.join(states)})")
    lines.append("")
    lines.append(
        "Use:  spc-plot --mode temperature   "
        "(direct USB; or --port temperature=HOST:PORT for a VirtualHub/YoctoHub)"
    )
    return "\n".join(lines)
