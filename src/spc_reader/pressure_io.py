"""Yoctopuce Yocto-4-20mA-Rx pressure transmitter I/O.

Reads raw loop current (mA) via ``YGenericSensor.get_signalValue()`` and maps
it to psi with a caller-supplied 2-point calibration (typically 0 psi @ 4 mA
to full-scale @ 20 mA). Device-side ``valueRange`` mapping is ignored so the
CLI calibration is always authoritative.

Shares the Yoctopuce hub / FreeAPI lifecycle with :mod:`yocto_io`.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from .spc_io import Reading
from .yocto_io import free_yocto_api, hub_target, register_hub

PRESSURE_FUNCTIONS = ("genericSensor1", "genericSensor2")
# Match temperature: re-scan while offline so late plug-in works.
_DEVLIST_MIN_S = 2.0
_MA_AT_4 = 4.0
_MA_AT_20 = 20.0
_MA_SPAN = _MA_AT_20 - _MA_AT_4


def _genericsensor():
    """Import YGenericSensor lazily so the package is optional at import time."""
    try:
        from yoctopuce.yocto_api import YAPI, YAPI_Exception, YRefParam
        from yoctopuce.yocto_genericsensor import YGenericSensor
    except ImportError:
        raise SystemExit(
            "yoctopuce package not found (needed for --mode pressure). "
            "Run:  pip install yoctopuce"
        )
    return YAPI, YAPI_Exception, YRefParam, YGenericSensor


@dataclass(frozen=True)
class PressureCalibration:
    """Linear map from loop current (mA) to pressure (psi)."""

    psi_at_4ma: float = 0.0
    psi_at_20ma: float = 0.0

    def ma_to_psi(self, ma: float) -> float:
        return self.psi_at_4ma + (ma - _MA_AT_4) / _MA_SPAN * (
            self.psi_at_20ma - self.psi_at_4ma
        )


def discover_pressure_modules() -> list[str]:
    """Serial numbers of modules exposing GenericSensor functions, in enum order."""
    YAPI, YAPI_Exception, YRefParam, YGenericSensor = _genericsensor()
    YAPI.UpdateDeviceList(YRefParam())
    serials: list[str] = []
    sensor = YGenericSensor.FirstGenericSensor()
    while sensor is not None:
        try:
            serial = sensor.get_module().get_serialNumber()
        except YAPI_Exception:
            serial = None
        if serial and serial not in serials:
            serials.append(serial)
        sensor = sensor.nextGenericSensor()
    return serials


class PressureChannel:
    """One Yocto-4-20mA-Rx input, read as raw loop current (mA).

    Opened on the main thread, then read exclusively from the sampler thread;
    close() runs after the sampler joins, so access is single-threaded at any
    instant.
    """

    def __init__(self, port: str, serial: str, input_n: int, cal: PressureCalibration):
        if input_n not in (1, 2):
            raise ValueError(f"pressure input must be 1 or 2, got {input_n}")
        YAPI, YAPI_Exception, YRefParam, YGenericSensor = _genericsensor()
        self._YAPI = YAPI
        self._YAPI_Exception = YAPI_Exception
        self._YRefParam = YRefParam
        self.port = port
        self.serial = serial
        self.input_n = input_n
        self.cal = cal
        fn = PRESSURE_FUNCTIONS[input_n - 1]
        self._sensor = YGenericSensor.FindGenericSensor(f"{serial}.{fn}")
        self._last_devlist = time.monotonic()
        self.last_ma: float | None = None

    def read(self) -> Reading | None:
        """Raw loop current in mA; None while the input is offline."""
        try:
            if not self._sensor.isOnline():
                self._refresh_devices()
                self.last_ma = None
                return None
            ma = self._sensor.get_signalValue()
        except self._YAPI_Exception:
            self._refresh_devices()
            self.last_ma = None
            return None
        self.last_ma = ma
        return Reading(ma, "mA")

    def read_psi(self) -> float:
        """Calibrated pressure in psi, or NaN while offline."""
        reading = self.read()
        if reading is None:
            return float("nan")
        return self.cal.ma_to_psi(reading.value)

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
        free_yocto_api()


def open_pressure_channel(
    port: str = "usb",
    *,
    serial: str | None = None,
    input_n: int = 1,
    cal: PressureCalibration,
) -> PressureChannel:
    """Connect to the hub and bind one GenericSensor input.

    With a pinned ``serial``, that module is bound even if currently offline
    (readings start when it appears). Otherwise the first discovered module
    is used; at least one must be online.
    """
    target = hub_target(port)
    register_hub(target)
    found = discover_pressure_modules()
    if serial:
        if serial not in found:
            print(
                f"  {serial} is not online yet — readings start when it appears.",
                file=sys.stderr,
            )
        return PressureChannel(target, serial, input_n, cal)
    if not found:
        raise OSError(
            f"No Yocto-4-20mA-Rx found on {target!r}.\n"
            "Plug in the module (direct USB needs exclusive access — quit "
            "VirtualHub if it is running), pass --port pressure=HOST:PORT "
            "for a hub, or pin --pressure-serial to start before plugging in."
        )
    return PressureChannel(target, found[0], input_n, cal)


def format_pressure_device_list() -> str:
    lines = ["Yoctopuce 4-20 mA modules (Yocto-4-20mA-Rx):"]
    try:
        from yoctopuce.yocto_api import YAPI_Exception
        from yoctopuce.yocto_genericsensor import YGenericSensor
    except ImportError:
        lines.append("  (yoctopuce package not installed — pip install yoctopuce)")
        return "\n".join(lines)
    try:
        register_hub("usb")
    except OSError as exc:
        lines.append(f"  ({exc})")
        return "\n".join(lines)
    serials = discover_pressure_modules()
    if not serials:
        lines.append(
            "  (none found — plug in Yocto-4-20mA-Rx; if VirtualHub is "
            "running it has claimed the module)"
        )
    else:
        for sn in serials:
            states = []
            for fn in PRESSURE_FUNCTIONS:
                sensor = YGenericSensor.FindGenericSensor(f"{sn}.{fn}")
                try:
                    if sensor.isOnline():
                        ma = sensor.get_signalValue()
                        states.append(f"{fn} {ma:.3f} mA")
                    else:
                        states.append(f"{fn} offline")
                except YAPI_Exception:
                    states.append(f"{fn} ?")
            lines.append(f"  {sn}  ({', '.join(states)})")
    lines.append("")
    lines.append(
        "Use:  spc-plot --mode pressure --pressure-at-20ma PSI   "
        "(direct USB; or --port pressure=HOST:PORT for a VirtualHub/YoctoHub)"
    )
    return "\n".join(lines)
