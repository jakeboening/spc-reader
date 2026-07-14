"""RS485 Modbus RTU load cell weighing transmitter I/O.

Supports transmitters using the common WTQ/TLB Modbus map (registers 40007–40014),
including generic RS485 load cell amplifiers sold with Modbus RTU output.

Configure the transmitter for Modbus RTU on RS485 (typically 9600 8N1, address 1).
Turn **485AutoSend OFF** — polling and continuous transmit interfere on half-duplex RS485.
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import time
from dataclasses import dataclass

from .mark10_io import ForceChannel
from .spc_io import Reading

DEFAULT_BAUD = 9600
DEFAULT_SLAVE_ID = 1

# Modbus register offsets (40001-based numbering minus 40001).
_REG_STATUS = 6       # 40007
_REG_GROSS_H = 7      # 40008
_REG_GROSS_L = 8      # 40009
_REG_DU = 13          # 40014 divisions / unit
# Fine weight on many RS485 transmitters (incl. B0D5CQ6VZ8-style): kg = reg / 2000.
_REG_FINE_WEIGHT = 26   # 40027
_FINE_WEIGHT_SCALE = 2000

# Raw ADC weight signal (32-bit, low word first). On transmitters configured for
# 0 decimal places the scaled weight registers only report whole kg, so we read
# this raw count and apply a user 2-point calibration instead. Empirically live
# and low-noise where 40009/40025/40027 are frozen or integer-only.
_REG_RAW_LO = 14   # 40015
_REG_RAW_HI = 15   # 40016
_GRAVITY = 9.80665

# Division index (L byte of 40014) → active decimal places.
_DECIMALS_BY_INDEX: dict[int, int] = {
    0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0,
    7: 1, 8: 1, 9: 1,
    10: 2, 11: 2, 12: 2,
    13: 3, 14: 3, 15: 3,
    16: 4, 17: 4, 18: 4,
}

# Unit index (H byte of 40014) → Reading unit string (see mark10_io._TO_N).
_UNIT_BY_INDEX: dict[int, str] = {
    0: "kg",
    1: "g",
    2: "t",
    3: "lbf",
    4: "n",
}

LOAD_CELL_RANGES_KG: dict[str, float] = {
    "10kg": 10.0,
    "20kg": 20.0,
    "50kg": 50.0,
    "100kg": 100.0,
    "200kg": 200.0,
    "500kg": 500.0,
    "1000kg": 1000.0,
}


@dataclass(frozen=True)
class LoadCellConfig:
    range_kg: float
    slave_id: int = DEFAULT_SLAVE_ID
    decimals: int | None = None
    debug: bool = False
    raw_cal: "RawCalibration | None" = None


def parse_loadcell_range(spec: str) -> float:
    """Parse a range like ``50kg`` or a key from :data:`LOAD_CELL_RANGES_KG`."""
    key = spec.strip().lower().replace(" ", "")
    if key in LOAD_CELL_RANGES_KG:
        return LOAD_CELL_RANGES_KG[key]
    if key.endswith("kg"):
        return float(key[:-2])
    raise ValueError(
        f"Invalid load cell range {spec!r}. "
        f"Use one of {', '.join(LOAD_CELL_RANGES_KG)} or e.g. 75kg."
    )


def range_choices() -> str:
    return ", ".join(LOAD_CELL_RANGES_KG)


def capacity_n(range_kg: float) -> float:
    return range_kg * 9.80665


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _char_delay_s(baud: int) -> float:
    """Modbus RTU time for one character (8N1 ≈ 11 bits)."""
    return 11.0 / baud


def _modbus_silence_s(baud: int) -> float:
    """Minimum idle time between Modbus RTU frames (3.5 character times)."""
    return 3.5 * _char_delay_s(baud)


def _find_fc03_response(buf: bytes, slave_id: int) -> bytes | None:
    """Locate a valid Modbus FC03 response (handles leading bus garbage / echo)."""
    for i in range(len(buf) - 4):
        if buf[i] != slave_id or buf[i + 1] != 0x03:
            continue
        byte_count = buf[i + 2]
        end = i + 3 + byte_count + 2
        if byte_count % 2 or end > len(buf):
            continue
        frame = buf[i:end]
        payload = frame[:-2]
        if _crc16(payload) == struct.unpack("<H", frame[-2:])[0]:
            return frame
    return None


def _read_holding_registers(
    ser,
    slave_id: int,
    start: int,
    count: int,
    *,
    baud: int = DEFAULT_BAUD,
    timeout_s: float = 0.5,
    post_tx_s: float | None = None,
) -> list[int]:
    """Modbus function 03; register addresses are 0-based (40007 → 6)."""
    # The read loop below is event-driven (scans for a CRC-valid response as
    # bytes arrive), so only the RTU inter-frame silence is needed after TX —
    # a longer fixed sleep just adds latency to every poll.
    if post_tx_s is None:
        post_tx_s = _modbus_silence_s(baud)

    pdu = struct.pack(">BBHH", slave_id, 0x03, start, count)
    frame = pdu + struct.pack("<H", _crc16(pdu))

    time.sleep(_modbus_silence_s(baud))
    ser.reset_input_buffer()
    ser.write(frame)
    ser.flush()
    time.sleep(post_tx_s)

    deadline = time.monotonic() + timeout_s
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            buf.extend(chunk)
            if _find_fc03_response(bytes(buf), slave_id):
                break
        elif buf:
            break

    resp = _find_fc03_response(bytes(buf), slave_id)
    if resp is None:
        return []

    byte_count = resp[2]
    if byte_count != count * 2:
        return []

    regs: list[int] = []
    for i in range(count):
        off = 3 + i * 2
        regs.append(struct.unpack(">H", resp[off : off + 2])[0])
    return regs


def _combine_weight_words(gross_h: int, gross_l: int) -> int:
    """Combine Modbus weight registers (32-bit or 16-bit-in-low-word layouts)."""
    if gross_h == 0 and gross_l != 0:
        return gross_l
    if gross_l == 0 and 0 < gross_h <= 999_999:
        return gross_h
    raw = (gross_h << 16) | gross_l
    if raw > 99_999_999:
        swapped = (gross_l << 16) | gross_h
        if swapped <= 99_999_999:
            return swapped
    return raw


def parse_modbus_weight(
    status: int,
    gross_h: int,
    gross_l: int,
    du: int | None = None,
    *,
    decimals: int | None = None,
    strict_status: bool = False,
) -> Reading | None:
    """Decode gross weight from Modbus registers."""
    # Many RS485 transmitters keep bit 0 set even while reporting valid weight.
    if strict_status and (status & 0x0003):
        return None

    raw = _combine_weight_words(gross_h, gross_l)
    if raw == 0 and gross_h == 0 and gross_l == 0:
        pass  # valid zero load
    if decimals is None and du is not None:
        div_index = du & 0xFF
        decimals = _DECIMALS_BY_INDEX.get(div_index, 0)
    if decimals is None:
        decimals = 0

    value = raw / (10 ** decimals)
    if status & 0x0080:
        value = -value

    unit = "kg"
    if du is not None:
        unit = _UNIT_BY_INDEX.get((du >> 8) & 0xFF, "kg")

    return Reading(value, unit)


def parse_fine_weight(raw: int, *, scale: int = _FINE_WEIGHT_SCALE) -> Reading:
    """Decode fine weight from register 40027 (value / 2000 → kg)."""
    if raw >= 0x8000:
        raw -= 0x10000
    return Reading(raw / scale, "kg")


def read_raw_counts(
    ser,
    slave_id: int,
    *,
    baud: int = DEFAULT_BAUD,
    timeout_s: float = 0.5,
) -> int | None:
    """Read the raw ADC weight counts from 40015/40016 (32-bit, low word first)."""
    regs = _read_holding_registers(
        ser, slave_id, _REG_RAW_LO, 2, baud=baud, timeout_s=timeout_s,
    )
    if len(regs) < 2:
        return None
    value = (regs[1] << 16) | regs[0]
    if value >= 0x8000_0000:
        value -= 0x1_0000_0000
    return value


@dataclass(frozen=True)
class RawCalibration:
    """2-point linear map from raw ADC counts to kilograms."""

    zero_counts: float
    counts_per_kg: float

    def to_kg(self, raw: int) -> float:
        if self.counts_per_kg == 0:
            return float("nan")
        return (raw - self.zero_counts) / self.counts_per_kg


def normalize_port(port: str) -> str:
    """Canonical serial port name (Windows COM names are case-insensitive)."""
    if sys.platform == "win32" and re.fullmatch(r"com\d+", port, re.IGNORECASE):
        return port.upper()
    return port


def port_present(port: str) -> bool:
    """Whether ``port`` currently exists (COM name on Windows, device node elsewhere)."""
    if sys.platform != "win32":
        return os.path.exists(port)
    # COM names are not filesystem paths (COM1–COM9 are reserved DOS device
    # names and COM10+ need a \\.\ prefix), so ask pyserial instead.
    try:
        from serial.tools import list_ports
    except ImportError:
        return True  # cannot enumerate; let open() decide
    want = port.upper()
    return any(info.device.upper() == want for info in list_ports.comports())


def default_cal_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "spc-reader", "loadcell_cal.json")


def load_calibration(port: str, *, path: str | None = None) -> RawCalibration | None:
    """Load a saved raw-ADC calibration for ``port`` (falls back to a ``default`` entry)."""
    port = normalize_port(port)
    path = path or default_cal_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    entry = data.get(port) or data.get("default")
    if not isinstance(entry, dict):
        return None
    try:
        return RawCalibration(
            float(entry["zero_counts"]), float(entry["counts_per_kg"])
        )
    except (KeyError, TypeError, ValueError):
        return None


def save_calibration(
    port: str,
    cal: RawCalibration,
    *,
    mass_kg: float | None = None,
    path: str | None = None,
) -> str:
    """Persist ``cal`` for ``port`` into the JSON config, returning the file path."""
    port = normalize_port(port)
    path = path or default_cal_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    entry = {
        "zero_counts": cal.zero_counts,
        "counts_per_kg": cal.counts_per_kg,
    }
    if mass_kg is not None:
        entry["mass_kg"] = mass_kg
    data[port] = entry
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return path


def read_weight(
    ser,
    slave_id: int,
    *,
    baud: int = DEFAULT_BAUD,
    timeout_s: float = 0.5,
    decimals: int | None = None,
) -> Reading | None:
    """Read weight, preferring fine register 40027 over coarse gross-weight block."""
    fine = _read_holding_registers(
        ser, slave_id, _REG_FINE_WEIGHT, 1, baud=baud, timeout_s=timeout_s,
    )
    if fine:
        reading = parse_fine_weight(fine[0])
        return reading

    regs = _read_holding_registers(
        ser, slave_id, _REG_STATUS, 4, baud=baud, timeout_s=timeout_s,
    )
    if len(regs) < 3:
        return None
    du_regs = _read_holding_registers(
        ser, slave_id, _REG_DU, 1, baud=baud, timeout_s=timeout_s,
    )
    du = du_regs[0] if du_regs else None
    return parse_modbus_weight(
        regs[0], regs[1], regs[2], du, decimals=decimals,
    )


@dataclass(frozen=True)
class ModbusProbeResult:
    start: int
    regs: list[int]
    reading: Reading | None


def probe_modbus(
    port: str,
    *,
    baud: int = DEFAULT_BAUD,
    slave_id: int = DEFAULT_SLAVE_ID,
    decimals: int | None = 2,
    attempts: int = 5,
) -> list[ModbusProbeResult]:
    """Try common register maps and return raw register snapshots."""
    import serial

    ser = serial.Serial(
        port,
        baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.2,
    )
    results: list[ModbusProbeResult] = []
    try:
        for start, count in ((6, 4), (0, 4), (7, 2), (0, 2)):
            regs: list[int] = []
            for _ in range(attempts):
                got = _read_holding_registers(
                    ser, slave_id, start, count, baud=baud, timeout_s=0.3,
                )
                if got:
                    regs = got
                    break
                time.sleep(0.05)
            reading = None
            if len(regs) >= 3:
                du = regs[3] if len(regs) > 3 else None
                reading = parse_modbus_weight(
                    regs[0], regs[1], regs[2], du, decimals=decimals,
                )
            elif len(regs) == 2:
                reading = parse_modbus_weight(0, regs[0], regs[1], decimals=decimals)
            results.append(ModbusProbeResult(start=start, regs=regs, reading=reading))
    finally:
        ser.close()
    return results


class LoadCellModbus:
    """RS485 Modbus RTU load cell transmitter."""

    def __init__(
        self,
        port: str,
        config: LoadCellConfig,
        baud: int = DEFAULT_BAUD,
        timeout: float = 0.5,
        settle_s: float = 0.0,
    ):
        import serial
        self.port = port
        self.config = config
        self._baud = baud
        self._settle_s = settle_s
        #: Raw ADC counts (register 40015/16) of the most recent read(),
        #: or None if unavailable. Lets loggers persist recalibratable data.
        self.last_counts: int | None = None
        self._ser = serial.Serial(
            port,
            baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
        )

    def close(self) -> None:
        if self._ser.is_open:
            self._ser.close()

    def _debug(self, msg: str) -> None:
        if self.config.debug:
            print(f"  loadcell: {msg}", file=sys.stderr)

    def read(self) -> Reading | None:
        if self._settle_s:
            time.sleep(self._settle_s)
        self.last_counts = None
        timeout_s = self._ser.timeout or 0.5
        for attempt in range(3):
            if self.config.raw_cal is not None:
                raw = read_raw_counts(
                    self._ser, self.config.slave_id,
                    baud=self._baud, timeout_s=timeout_s,
                )
                if raw is not None:
                    self.last_counts = raw
                    kg = self.config.raw_cal.to_kg(raw)
                    if self.config.debug:
                        self._debug(
                            f"raw={raw} → {kg:.4f} kg ({kg * _GRAVITY:.3f} N)"
                        )
                    return Reading(kg, "kg")
            else:
                reading = read_weight(
                    self._ser,
                    self.config.slave_id,
                    baud=self._baud,
                    timeout_s=timeout_s,
                    decimals=self.config.decimals,
                )
                if reading is not None:
                    # Also grab the raw ADC counts so logs stay recalibratable
                    # even without a saved calibration (costs one extra Modbus
                    # transaction per sample in this mode only).
                    self.last_counts = read_raw_counts(
                        self._ser, self.config.slave_id,
                        baud=self._baud, timeout_s=timeout_s,
                    )
                    if self.config.debug:
                        self._debug(
                            f"→ {reading.value:.3f} {reading.unit}"
                            f" ({reading.value * _GRAVITY:.3f} N)"
                            f"  raw={self.last_counts}"
                        )
                    return reading
            self._debug(f"attempt {attempt + 1}: no Modbus response")
            time.sleep(0.05)
        return None


def list_rs485_ports() -> list[tuple[str, str]]:
    """USB serial ports that may be RS485 adapters (excludes Mark-10)."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []

    from .mark10_io import _port_looks_like_mark10

    found: list[tuple[str, str]] = []
    for info in list_ports.comports():
        if _port_looks_like_mark10(info):
            continue
        dev = info.device
        base = dev.rsplit("/", 1)[-1]
        is_linux_usb = base.startswith("ttyUSB") or base.startswith("ttyACM")
        # macOS: USB serial adapters appear as /dev/cu.*; require a USB VID to
        # skip Bluetooth/debug pseudo-ports.
        is_mac_usb = base.startswith("cu.") and info.vid is not None
        # Windows: COMx; require a USB VID to skip motherboard UARTs.
        is_win_usb = base.upper().startswith("COM") and info.vid is not None
        if not (is_linux_usb or is_mac_usb or is_win_usb):
            continue
        label = info.description or "USB serial"
        if info.manufacturer:
            label = f"{label} ({info.manufacturer})"
        found.append((dev, label))
    return found


def format_loadcell_device_list() -> str:
    lines = [
        "RS485 load cell transmitter (Modbus RTU):",
        "  Configure transmitter for Modbus RTU (9600 8N1, addr 1).",
        "  Turn 485AutoSend OFF (half-duplex bus cannot poll while auto-transmitting).",
    ]
    ports = list_rs485_ports()
    if not ports:
        lines.append(
            "  (no /dev/ttyUSB*, /dev/ttyACM*, /dev/cu.usbserial-*, or USB COMx "
            "found — plug in USB-RS485 adapter)"
        )
    else:
        for path, label in ports:
            lines.append(f"  {path}  {label}")
    lines.extend([
        "",
        f"Load cell ranges: {range_choices()}",
        "",
        "Use:  spc-plot --force-type loadcell --force-port /dev/ttyUSB0 --loadcell-range 100kg",
        "      (macOS: --force-port /dev/cu.usbserial-XXXX, Windows: --force-port COM5)",
        "Probe:  spc-loadcell-probe --port /dev/ttyUSB0",
    ])
    return "\n".join(lines)


def open_loadcell_channel(
    port: str,
    *,
    range_kg: float,
    baud: int = DEFAULT_BAUD,
    slave_id: int = DEFAULT_SLAVE_ID,
    decimals: int | None = None,
    debug: bool = False,
    raw_cal: RawCalibration | None = None,
) -> ForceChannel:
    config = LoadCellConfig(
        range_kg=range_kg, slave_id=slave_id, decimals=decimals,
        debug=debug, raw_cal=raw_cal,
    )
    return LoadCellModbus(port, config, baud=baud)


def _sample_raw_median(
    ser, slave_id: int, samples: int, baud: int,
) -> float:
    import statistics
    vals: list[int] = []
    while len(vals) < samples:
        raw = read_raw_counts(ser, slave_id, baud=baud, timeout_s=0.3)
        if raw is not None:
            vals.append(raw)
        time.sleep(0.05)
    return statistics.median(vals)


def cal_main() -> None:
    import argparse

    p = argparse.ArgumentParser(
        description="Calibrate an RS485 load cell's raw ADC register (2-point: tare + known mass)",
    )
    p.add_argument("--port", required=True, metavar="PATH")
    p.add_argument("--mass", type=float, required=True, metavar="KG",
                   help="Known calibration mass in kg")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument("--addr", type=int, default=DEFAULT_SLAVE_ID, metavar="ID")
    p.add_argument("--samples", type=int, default=20,
                   help="Readings to median per point (default: 20)")
    p.add_argument("--config", default=None, metavar="PATH",
                   help=f"Calibration file (default: {default_cal_path()})")
    args = p.parse_args()

    if args.mass <= 0:
        sys.exit("--mass must be positive.")

    import serial

    ser = serial.Serial(
        args.port, args.baud,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=0.2,
    )
    try:
        input("Remove ALL load from the cell, then press Enter... ")
        zero = _sample_raw_median(ser, args.addr, args.samples, args.baud)
        print(f"  zero  = {zero:.0f} counts")
        input(f"Apply the {args.mass:g} kg mass, then press Enter... ")
        span = _sample_raw_median(ser, args.addr, args.samples, args.baud)
        print(f"  loaded = {span:.0f} counts")
    finally:
        ser.close()

    if span == zero:
        sys.exit(
            "Loaded and empty counts are identical — the raw register did not "
            "respond to load. Check wiring/register map."
        )
    counts_per_kg = (span - zero) / args.mass
    cal = RawCalibration(zero_counts=zero, counts_per_kg=counts_per_kg)
    path = save_calibration(args.port, cal, mass_kg=args.mass, path=args.config)
    print(f"  counts_per_kg = {counts_per_kg:.1f}")
    print(f"  full scale ≈ ±{abs(0x7FFF_FFFF / counts_per_kg):.0f} kg (raw 32-bit limit)")
    print(f"Saved calibration for {args.port} → {path}")


def probe_main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Probe RS485 load cell Modbus registers")
    p.add_argument("--port", required=True, metavar="PATH")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument("--addr", type=int, default=DEFAULT_SLAVE_ID, metavar="ID")
    p.add_argument("--decimals", type=int, default=None,
                   help="Override decimal places (default: from DU register)")
    p.add_argument("--attempts", type=int, default=8)
    args = p.parse_args()

    print(f"Probing {args.port}  addr={args.addr}  baud={args.baud}")
    print("(Ensure 485AutoSend is OFF and protocol is Modbus RTU.)\n")

    import serial

    ser = serial.Serial(
        args.port,
        args.baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.2,
    )
    try:
        print(f"Fine weight register 40027 (value / {_FINE_WEIGHT_SCALE} → kg):")
        ok = 0
        from .mark10_io import to_n
        for n in range(args.attempts):
            regs = _read_holding_registers(
                ser, args.addr, _REG_FINE_WEIGHT, 1, baud=args.baud, timeout_s=0.3,
            )
            if regs:
                ok += 1
                reading = parse_fine_weight(regs[0])
                print(
                    f"  [{n + 1}] 40027=0x{regs[0]:04X} ({regs[0]})"
                    f"  →  {reading.value:.3f} kg  ({to_n(reading):.3f} N)"
                )
            else:
                print(f"  [{n + 1}] (no response)")
            time.sleep(0.08)
        print(f"  success rate: {ok}/{args.attempts}\n")

        print("Coarse gross-weight block (40007–40010, legacy map):")
        for start, count in ((6, 4), (0, 4), (7, 2), (0, 2)):
            label = f"400{start + 1:02d}" if start < 94 else str(start)
            print(f"Read {count} regs @ offset {start} (40001+{start}):")
            ok = 0
            for n in range(args.attempts):
                regs = _read_holding_registers(
                    ser, args.addr, start, count, baud=args.baud, timeout_s=0.3,
                )
                if regs:
                    ok += 1
                    reading = None
                    if len(regs) >= 3:
                        du = regs[3] if len(regs) > 3 else None
                        reading = parse_modbus_weight(
                            regs[0], regs[1], regs[2], du, decimals=args.decimals,
                        )
                    elif len(regs) == 2:
                        reading = parse_modbus_weight(
                            0, regs[0], regs[1], decimals=args.decimals,
                        )
                    reg_str = " ".join(f"0x{r:04X}" for r in regs)
                    if reading:
                        from .mark10_io import to_n
                        print(
                            f"  [{n + 1}] {reg_str}  →  {reading.value} {reading.unit}"
                            f"  ({to_n(reading):.3f} N)"
                        )
                    else:
                        print(f"  [{n + 1}] {reg_str}  →  (parse failed)")
                else:
                    print(f"  [{n + 1}] (no response)")
                time.sleep(0.08)
            print(f"  success rate: {ok}/{args.attempts}\n")
    finally:
        ser.close()
