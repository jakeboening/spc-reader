"""Yoctopuce Yocto-4-20mA-Rx pressure transmitter I/O.

Reads raw loop current (mA) and maps it to psi with a caller-supplied 2-point
calibration (typically 0 psi @ 4 mA to full-scale @ 20 mA).

Loop current comes from ``YGenericSensor.get_signalValue()`` when that looks
like a live 4–20 mA reading. If the module was previously configured in
VirtualHub with an engineering ``valueRange`` (e.g. 0…5000 psi), a dead/zero
``signalValue`` is recovered by inverting ``get_currentValue()`` through the
configured ranges — otherwise a true 4 mA / 0 psi point becomes
``(0 − 4) / 16 × FS`` (e.g. −1250 psi at 5000 psi full scale).

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
# Plausible live loop currents (mA), including a little headroom past 20 mA.
_MA_LIVE_MIN = 0.5
_MA_LIVE_MAX = 24.0


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


def _finite_sensor_number(value: float, invalid: float) -> float | None:
    """Return value if it is a usable sensor reading, else None."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or abs(v) > 1e100:  # NaN / YAPI.INVALID_DOUBLE
        return None
    if invalid is not None and v == invalid:
        return None
    return v


def _parse_range(spec: str | None) -> tuple[float, float] | None:
    """Parse a Yoctopuce range string like ``4...20`` or ``0...5000``."""
    if not spec or "..." not in spec:
        return None
    left, right = spec.split("...", 1)
    try:
        return float(left.strip()), float(right.strip())
    except ValueError:
        return None


def _ranges_are_identity(
    signal_range: tuple[float, float], value_range: tuple[float, float]
) -> bool:
    (s0, s1), (v0, v1) = signal_range, value_range
    return abs(s0 - v0) < 1e-6 and abs(s1 - v1) < 1e-6


def _invert_value_to_ma(
    current_value: float,
    signal_range: tuple[float, float],
    value_range: tuple[float, float],
) -> float:
    s0, s1 = signal_range
    v0, v1 = value_range
    if abs(v1 - v0) < 1e-12:
        return s0
    return s0 + (current_value - v0) / (v1 - v0) * (s1 - s0)


def _is_live_ma(ma: float | None) -> bool:
    return ma is not None and _MA_LIVE_MIN <= ma <= _MA_LIVE_MAX


def read_loop_ma(sensor) -> float | None:
    """Best-effort loop current in mA from a YGenericSensor.

    Prefers ``get_signalValue()``. If that is not a plausible loop current but
    the module has a non-identity engineering ``valueRange``, inverts
    ``get_currentValue()`` back to mA so a VirtualHub psi mapping cannot be
    mistaken for milliamps.
    """
    YAPI, YAPI_Exception, _, YGenericSensor = _genericsensor()
    try:
        if not sensor.isOnline():
            return None
        sig = _finite_sensor_number(
            sensor.get_signalValue(), YGenericSensor.SIGNALVALUE_INVALID
        )
        cur = _finite_sensor_number(sensor.get_currentValue(), YAPI.INVALID_DOUBLE)
        signal_range = _parse_range(sensor.get_signalRange())
        value_range = _parse_range(sensor.get_valueRange())
    except YAPI_Exception:
        return None

    if _is_live_ma(sig):
        return sig

    if (
        cur is not None
        and signal_range is not None
        and value_range is not None
        and not _ranges_are_identity(signal_range, value_range)
    ):
        ma = _invert_value_to_ma(cur, signal_range, value_range)
        if _is_live_ma(ma) or (sig is not None and sig < _MA_LIVE_MIN):
            # Engineering mapping: 0 psi @ 4 mA often reports currentValue=0,
            # which must not be treated as 0 mA.
            return ma

    if _is_live_ma(cur) and (
        signal_range is None
        or value_range is None
        or _ranges_are_identity(signal_range, value_range)
    ):
        return cur

    # Last resort: return the raw signal even if it looks like an open circuit
    # (≈0 mA), so the caller can log it and the UI can warn.
    return sig if sig is not None else cur


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


def _probe_inputs(serial: str) -> dict[int, float | None]:
    """Read both inputs once; values are mA or None if offline."""
    _, YAPI_Exception, _, YGenericSensor = _genericsensor()
    out: dict[int, float | None] = {}
    for n, fn in enumerate(PRESSURE_FUNCTIONS, start=1):
        sensor = YGenericSensor.FindGenericSensor(f"{serial}.{fn}")
        try:
            out[n] = read_loop_ma(sensor) if sensor.isOnline() else None
        except YAPI_Exception:
            out[n] = None
    return out


def _pick_input(serial: str, requested: int | None) -> int:
    """Choose input 1 or 2; auto-pick a live loop when ``requested`` is None."""
    probed = _probe_inputs(serial)
    if requested is not None:
        other = 2 if requested == 1 else 1
        if not _is_live_ma(probed.get(requested)) and _is_live_ma(probed.get(other)):
            print(
                f"  Warning: pressure input {requested} reads "
                f"{_fmt_ma(probed.get(requested))}, but input {other} reads "
                f"{_fmt_ma(probed.get(other))}. "
                f"Try --pressure-input {other}.",
                file=sys.stderr,
            )
        return requested

    live = [n for n in (1, 2) if _is_live_ma(probed.get(n))]
    if len(live) == 1:
        print(
            f"  Pressure auto-selected input {live[0]} "
            f"({_fmt_ma(probed[live[0]])})",
            file=sys.stderr,
        )
        return live[0]
    if len(live) == 2:
        print(
            f"  Pressure inputs both live "
            f"(1={_fmt_ma(probed[1])}, 2={_fmt_ma(probed[2])}); "
            "using input 1 (pass --pressure-input 2 to override).",
            file=sys.stderr,
        )
        return 1
    print(
        f"  Warning: no live 4–20 mA signal on {serial} "
        f"(input1={_fmt_ma(probed.get(1))}, input2={_fmt_ma(probed.get(2))}). "
        "Check wiring / --pressure-input.",
        file=sys.stderr,
    )
    return 1


def _fmt_ma(ma: float | None) -> str:
    return "offline" if ma is None else f"{ma:.3f} mA"


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
        # Snapshot of module mapping for the startup panel / attrs.
        try:
            self.signal_range = self._sensor.get_signalRange()
            self.value_range = self._sensor.get_valueRange()
            self.signal_unit = self._sensor.get_signalUnit()
            self.value_unit = self._sensor.get_unit()
        except YAPI_Exception:
            self.signal_range = ""
            self.value_range = ""
            self.signal_unit = ""
            self.value_unit = ""

    def read(self) -> Reading | None:
        """Raw loop current in mA; None while the input is offline."""
        try:
            if not self._sensor.isOnline():
                self._refresh_devices()
                self.last_ma = None
                return None
            ma = read_loop_ma(self._sensor)
        except self._YAPI_Exception:
            self._refresh_devices()
            self.last_ma = None
            return None
        if ma is None:
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
    input_n: int | None = None,
    cal: PressureCalibration,
) -> PressureChannel:
    """Connect to the hub and bind one GenericSensor input.

    With a pinned ``serial``, that module is bound even if currently offline
    (readings start when it appears). Otherwise the first discovered module
    is used; at least one must be online.

    ``input_n`` of ``None`` auto-selects an input that currently shows a live
    4–20 mA loop current.
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
            chosen = input_n if input_n is not None else 1
            return PressureChannel(target, serial, chosen, cal)
        chosen = _pick_input(serial, input_n)
        return PressureChannel(target, serial, chosen, cal)
    if not found:
        raise OSError(
            f"No Yocto-4-20mA-Rx found on {target!r}.\n"
            "Plug in the module (direct USB needs exclusive access — quit "
            "VirtualHub if it is running), pass --port pressure=HOST:PORT "
            "for a hub, or pin --pressure-serial to start before plugging in."
        )
    serial = found[0]
    chosen = _pick_input(serial, input_n)
    return PressureChannel(target, serial, chosen, cal)


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
                        ma = read_loop_ma(sensor)
                        cur = sensor.get_currentValue()
                        unit = sensor.get_unit()
                        vrange = sensor.get_valueRange()
                        states.append(
                            f"{fn} loop={_fmt_ma(ma)} "
                            f"value={cur:.3f} {unit} [{vrange}]"
                        )
                    else:
                        states.append(f"{fn} offline")
                except YAPI_Exception:
                    states.append(f"{fn} ?")
            lines.append(f"  {sn}")
            for state in states:
                lines.append(f"    {state}")
    lines.append("")
    lines.append(
        "Use:  spc-plot --mode pressure --pressure-at-20ma PSI   "
        "(direct USB; or --port pressure=HOST:PORT for a VirtualHub/YoctoHub)"
    )
    return "\n".join(lines)
