"""Mitutoyo Digimatic SPC I/O for Linux.

Supports:
  - USB-ITN (0fe7:4001): HID device, VCP via USB control + interrupt IN (pyusb)
  - Virtual COM ports (IT-016U, RS-232 bridges): pyserial, ``1`` + CR request
"""

from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import dataclass
from typing import Protocol

MITUTOYO_VID = 0x0FE7
USB_ITN_PID = 0x4001
REQUEST_CMD = b"1\r"
UNITS = {0: "mm", 1: "in"}

DISPLACEMENT_DATASET = "displacement_mm"
FORCE_DATASET = "force_n"
LBF_TO_N = 4.4482216152605
DEFAULT_LOG_FILENAME = "force_and_displacement.h5"
# Legacy log filenames (still readable via --data-file):
LEGACY_LOG_FILENAMES = ("displacement_log.h5", "temperature_log.h5")
# Legacy HDF5 dataset names (still readable for old log files):
_LEGACY_DISPLACEMENT_KEYS = (DISPLACEMENT_DATASET, "temperature1", "temperature")


@dataclass(frozen=True)
class Reading:
    value: float
    unit: str


@dataclass(frozen=True)
class DeviceInfo:
    kind: str          # "usb-itn" | "serial" | "hidraw"
    path: str          # e.g. usb-itn:40006743, /dev/ttyUSB0, /dev/hidraw4
    serial: str
    product: str
    note: str = ""


class SPCChannel(Protocol):
    port: str

    def read(self) -> Reading | None: ...
    def close(self) -> None: ...


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_nibbles(nibbles: list[int]) -> Reading | None:
    if len(nibbles) != 13:
        return None
    for n in range(4):
        if nibbles[n] != 15:
            return None
    if nibbles[4] not in (0, 8):
        return None
    positive = nibbles[4] == 0
    number = sum(nibbles[i] * (10 ** (10 - i)) for i in range(5, 11))
    number /= 10 ** nibbles[11]
    unit = UNITS.get(nibbles[12], "mm")
    value = number if positive else -number
    if number == 0:
        value = 0.0
    return Reading(value, unit)


def parse_hex_frame(text: str) -> Reading | None:
    raw = re.sub(r"[^0-9a-fA-F]", "", text)
    if len(raw) < 13:
        return None
    try:
        nibbles = [int(raw[i], 16) for i in range(13)]
    except ValueError:
        return None
    return parse_nibbles(nibbles)


def parse_text_line(line: str) -> Reading | None:
    line = line.strip()
    if not line:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{13,}", line.replace(" ", "")):
        return parse_hex_frame(line)
    m = re.match(
        r"^(?:[0-9]{2}[A-Z])?([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(mm|in)?",
        line,
        re.IGNORECASE,
    )
    if m:
        unit = (m.group(2) or "mm").lower()
        return Reading(float(m.group(1)), unit)
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", line)
    if m:
        return Reading(float(m.group(1)), "mm")
    return None


# ── Discovery ─────────────────────────────────────────────────────────────────

def _hidraw_for_mitutoyo() -> list[DeviceInfo]:
    found: list[DeviceInfo] = []
    for node in sorted(glob.glob("/dev/hidraw*")):
        props = _udev_props(node)
        if props.get("ID_VENDOR_ID", "").lower() != "0fe7":
            continue
        serial = props.get("ID_SERIAL_SHORT", "")
        found.append(DeviceInfo(
            kind="hidraw",
            path=node,
            serial=serial,
            product=props.get("ID_MODEL", "USB-ITN"),
            note="HID keyboard mode; use usb-itn path for pyusb reads",
        ))
    return found


def _udev_props(devpath: str) -> dict[str, str]:
    import subprocess
    try:
        out = subprocess.run(
            ["udevadm", "info", "-q", "property", "-n", devpath],
            capture_output=True, text=True, check=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    props: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    return props


def _usb_sysfs_serial(dev: object) -> str:
    """Read serial from sysfs when iSerial string fetch is denied."""
    import usb.util
    try:
        return usb.util.get_string(dev, dev.iSerialNumber) or ""  # type: ignore[attr-defined]
    except Exception:
        pass
    for node in glob.glob("/sys/bus/usb/devices/*"):
        try:
            if int(open(f"{node}/idVendor").read(), 16) != MITUTOYO_VID:
                continue
            if int(open(f"{node}/idProduct").read(), 16) != USB_ITN_PID:
                continue
            return open(f"{node}/serial").read().strip()
        except (OSError, ValueError):
            continue
    return ""


def find_usb_itn(serial: str | None = None) -> list[DeviceInfo]:
    """USB-ITN adapters visible on the USB bus (pyusb)."""
    try:
        import usb.core
    except ImportError:
        return []

    devices: list[DeviceInfo] = []
    for dev in usb.core.find(find_all=True, idVendor=MITUTOYO_VID, idProduct=USB_ITN_PID) or []:
        sn = _usb_sysfs_serial(dev)
        if serial and sn != serial:
            continue
        path = f"usb-itn:{sn}" if sn else "usb-itn"
        devices.append(DeviceInfo(
            kind="usb-itn",
            path=path,
            serial=sn,
            product="USB-ITN",
            note=f"bus {dev.bus:03d} addr {dev.address:03d}",
        ))
    return devices


def list_serial_ports() -> list[DeviceInfo]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []

    found: list[DeviceInfo] = []
    for info in list_ports.comports():
        desc = f"{info.description} {info.manufacturer or ''}".lower()
        if "mitutoyo" not in desc and "usb-it" not in desc and "digimatic" not in desc:
            continue
        found.append(DeviceInfo(
            kind="serial",
            path=info.device,
            serial=info.serial_number or "",
            product=info.description or "serial",
        ))
    return found


def discover_devices() -> list[DeviceInfo]:
    """All Mitutoyo-related interfaces on this machine."""
    seen: set[str] = set()
    out: list[DeviceInfo] = []
    for dev in find_usb_itn() + list_serial_ports() + _hidraw_for_mitutoyo():
        if dev.path in seen:
            continue
        seen.add(dev.path)
        out.append(dev)
    return out


def read_displacement_mm(grp) -> "np.ndarray":
    """Load displacement samples from a cycle group (current or legacy dataset names)."""
    import numpy as np
    for key in _LEGACY_DISPLACEMENT_KEYS:
        if key in grp:
            return grp[key][:]
    raise KeyError(
        f"no displacement dataset in cycle (expected one of {_LEGACY_DISPLACEMENT_KEYS})"
    )


def read_force_n(grp) -> "np.ndarray | None":
    """Load force samples in newtons if present; otherwise None."""
    import numpy as np
    if FORCE_DATASET in grp:
        return grp[FORCE_DATASET][:]
    if "force_lbf" in grp:
        return grp["force_lbf"][:] * LBF_TO_N
    return None


def read_force_lbf(grp) -> "np.ndarray | None":
    """Legacy helper: force in lbf (converts from ``force_n`` if needed)."""
    n = read_force_n(grp)
    if n is None:
        return None
    return n / LBF_TO_N


def list_candidate_ports() -> list[str]:
    """Paths suitable for --port (prefer usb-itn over hidraw)."""
    usb = find_usb_itn()
    if usb:
        return [d.path for d in usb]
    return [d.path for d in list_serial_ports()]


def auto_port() -> str | None:
    usb = find_usb_itn()
    if len(usb) == 1:
        return usb[0].path
    serial = list_serial_ports()
    if len(serial) == 1:
        return serial[0].path
    return None


def format_device_list() -> str:
    lines = ["Mitutoyo devices:"]
    devices = discover_devices()
    if not devices:
        lines.append("  (none found on USB or serial)")
    else:
        for d in devices:
            extra = f"  ({d.note})" if d.note else ""
            lines.append(f"  [{d.kind}] {d.path}  serial={d.serial!r}  {d.product}{extra}")
    lines.append("")
    lines.append("Not Mitutoyo (often confused with a disconnected adapter):")
    lines.append("  eth0 — DisplayLink Humanscale M-Connect 2 USB dock Ethernet")
    lines.append("  enp0s31f6 — built-in Ethernet (no cable)")
    lines.append("")
    lines.append("Use:  --port usb-itn   (or the path shown above)")
    return "\n".join(lines)


# ── USB-ITN (pyusb) ───────────────────────────────────────────────────────────

class USBITN:
    """Mitutoyo USB-ITN via control + interrupt endpoints (not a tty)."""

    def __init__(self, serial: str | None = None):
        import usb.core
        import usb.util

        devs = list(usb.core.find(find_all=True, idVendor=MITUTOYO_VID, idProduct=USB_ITN_PID) or [])
        if not devs:
            raise OSError(
                "No USB-ITN found (id 0fe7:4001). Is the cable plugged in?"
            )
        dev = devs[0]
        if serial:
            for d in devs:
                if _usb_sysfs_serial(d) == serial:
                    dev = d
                    break
            else:
                raise OSError(f"No USB-ITN with serial {serial!r}")

        sn = _usb_sysfs_serial(dev)
        self.port = f"usb-itn:{sn}" if sn else "usb-itn"
        self._dev = dev
        self._opened = False

    def _ensure_open(self) -> None:
        if self._opened:
            return
        dev = self._dev
        if dev.is_kernel_driver_active(0):
            try:
                dev.detach_kernel_driver(0)
            except Exception:
                pass
        dev.set_configuration()
        dev.ctrl_transfer(0x40, 0x01, 0xA5A5, 0)
        dev.ctrl_transfer(0xC0, 0x02, 0, 0, 1)
        self._opened = True

    def read(self) -> Reading | None:
        self._ensure_open()
        dev = self._dev
        dev.ctrl_transfer(0x40, 0x03, 0, 0, REQUEST_CMD)
        try:
            raw = dev.read(0x81, 64, timeout=2000)
        except Exception:
            return None
        if not raw:
            return None
        text = bytes(raw).decode("ascii", errors="replace").strip("\x00")
        return parse_text_line(text)

    def close(self) -> None:
        import usb.util
        try:
            usb.util.dispose_resources(self._dev)
        except Exception:
            pass


# ── Serial ────────────────────────────────────────────────────────────────────

class SPCSerial:
    def __init__(
        self,
        port: str,
        baud: int = 9600,
        timeout: float = 0.5,
        settle_s: float = 0.05,
    ):
        import serial
        self.port = port
        self._settle_s = settle_s
        self._ser = serial.Serial(port, baud, timeout=timeout)

    def close(self) -> None:
        if self._ser.is_open:
            self._ser.close()

    def read(self) -> Reading | None:
        self._ser.reset_input_buffer()
        self._ser.write(REQUEST_CMD)
        time.sleep(self._settle_s)
        raw = self._ser.readline() or self._ser.read(32)
        if not raw:
            return None
        return parse_text_line(raw.decode("ascii", errors="replace"))


def open_channel(spec: str, baud: int = 9600) -> SPCChannel:
    """Open one gauge from a port spec (usb-itn, usb-itn:SERIAL, /dev/tty*)."""
    if spec.startswith("usb-itn"):
        serial = spec.split(":", 1)[1] if ":" in spec else None
        return USBITN(serial=serial or None)
    return SPCSerial(spec, baud=baud)
