"""Mark-10 Series 5 force gauge I/O (GCL2 over USB virtual serial).

The M5-10 and other Series 5 models expose a USB CDC/FTDI serial port when
Serial/USB → USB is selected on the gauge. Poll with ``?C`` + CR for the
real-time reading (see Mark-10 Series 5 manual, GCL2).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Protocol

from .spc_io import Reading

CMD_REALTIME = b"?C\r"
DEFAULT_BAUD = 115200

# Newtons per gauge unit (multiply reading value by factor to get N)
_TO_N = {
    "n": 1.0,
    "lbf": 4.4482216152605,
    "ozf": 4.4482216152605 / 16.0,
    "gf": 0.00980665,
    "kgf": 9.80665,
    "kg": 9.80665,
    "g": 0.00980665,
    "t": 9806.65,
    "kn": 1000.0,
    "mn": 0.001,
}

_GCL2_RE = re.compile(
    r"^([+-]?\d+(?:\.\d+)?)\s*([A-Za-z]+)?",
)


@dataclass(frozen=True)
class ForceDeviceInfo:
    path: str
    serial: str
    product: str


class ForceChannel(Protocol):
    port: str

    def read(self) -> Reading | None: ...
    def close(self) -> None: ...


def parse_gcl2_line(line: str) -> Reading | None:
    """Parse a GCL2 reading line, e.g. ``-0.486 lbF`` or ``1.724 N``."""
    line = line.strip()
    if not line or line.startswith("*"):
        return None
    m = _GCL2_RE.match(line)
    if not m:
        return None
    unit = (m.group(2) or "n").lower()
    return Reading(float(m.group(1)), unit)


def to_n(reading: Reading | None) -> float:
    if reading is None:
        return float("nan")
    factor = _TO_N.get(reading.unit.lower().replace(" ", ""))
    if factor is None:
        return float("nan")
    return reading.value * factor


def _port_looks_like_mark10(info) -> bool:
    blob = " ".join(
        x or ""
        for x in (
            info.description,
            info.manufacturer,
            info.product,
            info.interface,
        )
    ).lower()
    return "mark-10" in blob or "mark10" in blob or "series 5" in blob


def list_mark10_ports() -> list[ForceDeviceInfo]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []

    found: list[ForceDeviceInfo] = []
    for info in list_ports.comports():
        if not _port_looks_like_mark10(info):
            continue
        found.append(ForceDeviceInfo(
            path=info.device,
            serial=info.serial_number or "",
            product=info.description or "Mark-10 Series 5",
        ))
    return found


def auto_force_port() -> str | None:
    ports = list_mark10_ports()
    if len(ports) == 1:
        return ports[0].path
    return None


def format_force_device_list() -> str:
    lines = ["Mark-10 force gauge (USB serial):"]
    ports = list_mark10_ports()
    if not ports:
        lines.append("  (none found — enable USB in Serial/USB settings on the gauge)")
    else:
        for d in ports:
            lines.append(f"  {d.path}  serial={d.serial!r}  {d.product}")
    lines.append("")
    lines.append("Use:  spc-plot --mode force   (auto-detects; or --port force=/dev/ttyUSB0)")
    return "\n".join(lines)


class Mark10Serial:
    """Series 5 gauge on a virtual COM port (GCL2)."""

    def __init__(
        self,
        port: str,
        baud: int = DEFAULT_BAUD,
        timeout: float = 0.5,
        settle_s: float = 0.02,
    ):
        import serial
        self.port = port
        self._settle_s = settle_s
        self._ser = serial.Serial(port, baud, timeout=timeout)

    def close(self) -> None:
        if self._ser.is_open:
            self._ser.close()

    def read(self) -> Reading | None:
        # Prefer auto-output lines already in the buffer (do not flush them).
        if self._ser.in_waiting:
            raw = self._ser.readline()
        else:
            self._ser.write(CMD_REALTIME)
            time.sleep(self._settle_s)
            raw = self._ser.readline()
            if not raw:
                raw = self._ser.read(64)
        if not raw:
            return None
        return parse_gcl2_line(raw.decode("ascii", errors="replace"))


def open_force_channel(port: str, baud: int = DEFAULT_BAUD) -> ForceChannel:
    return Mark10Serial(port, baud=baud)
