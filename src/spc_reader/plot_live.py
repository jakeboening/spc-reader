#!/usr/bin/env python3
"""Real-time multi-mode plotter and HDF5 logger.

Modes (one y-axis each, any combination): displacement (Mitutoyo SPC),
force (Mark-10 or RS485 load cell), temperature (Yoctopuce thermocouple).
"""

import argparse
import collections
import datetime
import os
import queue
import re
import shutil
import signal
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field

from . import mpl_setup  # noqa: F401  # before matplotlib.pyplot

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from .widgets import EditableTextBox

try:
    import h5py
except ImportError:
    sys.exit("h5py not found. Run:  pip install h5py  (or: pip install -r requirements.txt)")

from .loadcell_io import (
    RawCalibration,
    capacity_n,
    default_cal_path,
    format_loadcell_device_list,
    list_rs485_ports,
    load_calibration,
    normalize_port,
    open_loadcell_channel,
    parse_loadcell_range,
    port_present,
    range_choices,
)
from .mark10_io import (
    ForceChannel,
    auto_force_port,
    format_force_device_list,
    open_force_channel,
    to_n,
)
from .paths import default_log_path, resolve_data_path
from .spc_io import (
    DISPLACEMENT_DATASET,
    FORCE_COUNTS_DATASET,
    FORCE_DATASET,
    Reading,
    SPCChannel,
    auto_port,
    format_device_list,
    open_channel,
    temp_dataset,
)
from .yocto_io import (
    TEMP_FUNCTIONS,
    TemperatureChannel,
    format_temperature_device_list,
    open_temperature_channels,
    to_c,
)

MODES = ("force", "displacement", "temperature")

DISP_COLOR = "#eb6834"   # orange — displacement series
FORCE_COLOR = "#2a78d6"  # blue — force series
# Thermocouple palette, cycled TC1, TC2, ... across however many modules are
# connected; hues chosen to stay distinct from the orange/blue above.
TEMP_COLORS = ("#9333ea", "#0d9488", "#db2777", "#65a30d", "#b45309", "#64748b")
SURFACE = "#fcfcfb"      # window background
PANEL = "#ffffff"        # plot area
INK = "#1c1917"          # primary text
INK_MUTED = "#79716b"    # secondary text (axis titles, hints)
BORDER = "#d6d3d1"       # spines, box borders
GRID = "#ececea"         # y gridlines
ALERT = "#b42318"        # stopped banner, unsaved marker
WARN = "#b54708"         # sensor-disconnected banner
REC_GREEN = "#067647"    # recording chip
UNIT_MM = "mm"
UNIT_N = "N"
UNIT_C = "°C"
# M5-10 full scale: 10 lbf ≈ 44.5 N
DEFAULT_FORCE_MAX_N = 10 * 4.4482216152605
# Load cell default view: positive-only, 0 → 250 N.
DEFAULT_LOADCELL_MAX_N = 250.0
# Cap points actually drawn per line; the screen is ~1300 px wide, so drawing a
# full 600 s × 20 Hz window (12 000 pts) is wasted work. Full-rate data is still
# logged to HDF5 — this only thins the live view.
MAX_DRAW_POINTS = 2000
# While the load cell adapter is absent, probe for its port at this interval.
FORCE_RECONNECT_POLL_S = 0.5


def to_mm(reading: Reading | None) -> float:
    if reading is None:
        return float("nan")
    if reading.unit == "in":
        return reading.value * 25.4
    return reading.value


@dataclass
class Series:
    """One plotted line; ``key`` doubles as HDF5 dataset name and sample-dict key."""
    key: str
    label: str
    color: str
    decimals: int            # readout/hover precision
    line: object = None      # Line2D, set when the axes are built
    vals: collections.deque = field(default_factory=collections.deque)


@dataclass
class Mode:
    """One measurement kind, owning a y-axis and its line series."""
    name: str
    unit: str
    axis_color: str          # y-axis tint when several modes share the plot
    ylim: tuple
    series: list
    ax: object = None        # Axes, set when the figure is built


class DataLogger:
    """Buffers and flushes time + per-channel sample columns to HDF5."""

    FLUSH_EVERY = 25

    def __init__(
        self,
        filepath: str,
        hz: float,
        tag: str,
        *,
        columns: list[tuple[str, str]],
        attrs: dict | None = None,
    ):
        """``columns`` are (dataset_name, dtype) pairs, excluding ``time``;
        ``attrs`` are extra cycle-group attributes."""
        if not columns:
            raise ValueError("DataLogger needs at least one column")
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        self._path = filepath
        self._hz = hz
        self._tag = tag
        self._columns = list(columns)
        self._attrs = dict(attrs or {})
        self._buf: dict[str, list[float]] = {
            name: [] for name in ("time", *(c[0] for c in self._columns))
        }
        self._start_cycle()

    def _start_cycle(self) -> None:
        with h5py.File(self._path, "a") as f:
            cycles = f.require_group("cycles")
            cycle_idx = len(cycles) + 1
            grp_name = f"{cycle_idx:06d}"
            grp = cycles.create_group(grp_name)
            grp.attrs["tag"]       = self._tag or ""
            grp.attrs["label"]     = ""
            grp.attrs["hz"]        = self._hz
            grp.attrs["start_iso"] = datetime.datetime.now().isoformat()
            for key, val in self._attrs.items():
                grp.attrs[key] = val
            for ds_name, dtype in [("time", "f8"), *self._columns]:
                grp.create_dataset(
                    ds_name, shape=(0,), maxshape=(None,),
                    dtype=dtype, chunks=(256,), compression="gzip",
                )
            self._grp_path = f"cycles/{grp_name}"
            self._cycle_name = grp_name
            self._label = ""

    @property
    def cycle_name(self) -> str:
        """Current cycle group name, e.g. ``000001`` (matches the HDF5 group)."""
        return self._cycle_name

    @property
    def label(self) -> str:
        """Free-text label describing the current cycle (resets each new cycle)."""
        return self._label

    def set_label(self, text: str) -> None:
        """Persist a free-text label onto the current cycle's HDF5 group."""
        text = (text or "").strip()
        if text == self._label:
            return
        self._label = text
        with h5py.File(self._path, "a") as f:
            f[self._grp_path].attrs["label"] = text

    def new_cycle(self) -> str:
        """Flush the current cycle and begin a new HDF5 group."""
        self._flush()
        self._start_cycle()
        return self._grp_path

    def append(self, unix_t: float, values: dict[str, float | None]) -> None:
        """Buffer one sample row; keys missing from ``values`` log as NaN."""
        self._buf["time"].append(unix_t)
        for name, _dtype in self._columns:
            v = values.get(name)
            self._buf[name].append(float(v) if v is not None else float("nan"))
        if len(self._buf["time"]) >= self.FLUSH_EVERY:
            self._flush()

    def flush(self) -> None:
        """Persist buffered samples of the current cycle without starting a new one."""
        self._flush()

    def close(self) -> None:
        self._flush()

    def _flush(self) -> None:
        if not self._buf["time"]:
            return
        with h5py.File(self._path, "a") as f:
            grp = f[self._grp_path]
            for ds_name, data in self._buf.items():
                ds = grp[ds_name]
                n = len(ds)
                ds.resize(n + len(data), axis=0)
                ds[n:] = data
                data.clear()


def resolve_ports(p: argparse.ArgumentParser, modes: list[str],
                  port_args: list[str]) -> dict[str, str | None]:
    """Map --port entries onto active modes; None means auto-detect.

    Entries are ``MODE=VALUE`` or a bare ``VALUE`` (allowed once, binding to
    the only active mode without an explicit port). A ``word=`` prefix that
    isn't a mode name is rejected; values containing ``=`` for other reasons
    (paths, serials) pass through as bare values.
    """
    ports: dict[str, str | None] = {m: None for m in modes}
    bare: list[str] = []
    for entry in port_args:
        mode, sep, value = entry.partition("=")
        if sep and mode in MODES:
            if mode not in ports:
                p.error(f"--port {entry}: mode {mode!r} is not in --mode")
            if ports[mode] is not None:
                p.error(f"--port: duplicate port for mode {mode!r}")
            if not value:
                p.error(f"--port {entry}: empty port value")
            ports[mode] = value
        elif sep and re.fullmatch(r"[a-z]+", mode):
            p.error(f"--port {entry}: unknown mode {mode!r} "
                    f"(choose from {', '.join(MODES)})")
        else:
            bare.append(entry)
    if len(bare) > 1:
        p.error("--port: at most one bare value; use --port MODE=VALUE")
    if bare:
        unassigned = [m for m in modes if ports[m] is None]
        if not unassigned:
            p.error(f"--port {bare[0]}: every mode already has a port")
        if len(unassigned) > 1:
            p.error(f"--port {bare[0]} is ambiguous with modes "
                    f"{', '.join(unassigned)}; use --port MODE=VALUE")
        ports[unassigned[0]] = bare[0]
    return ports


def parse_args():
    p = argparse.ArgumentParser(
        description="Displacement (Mitutoyo SPC), force (Mark-10 or RS485 load cell), "
                    "and temperature (Yoctopuce thermocouple) logger with live plotting",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--mode", nargs="+", choices=MODES, metavar="MODE",
                   help="Channels to plot/log: force, displacement, temperature. "
                        "The first mode owns the left y-axis; each further mode "
                        "adds its own y-axis on the right.")
    p.add_argument("--port", action="append", default=[], metavar="[MODE=]VALUE",
                   help="Port for a mode, e.g. --port force=COM5 --port temperature=usb. "
                        "Repeatable; a bare VALUE binds to the only portless mode; "
                        "omitted ports auto-detect. Displacement: usb-itn[:SERIAL] "
                        "or serial port; force: serial port; temperature: usb or "
                        "HOST:PORT of a VirtualHub/YoctoHub.")
    p.add_argument("--baud", type=int, default=9600,
                   help="Displacement serial baud")
    p.add_argument("--list-ports", action="store_true",
                   help="List Mitutoyo, Mark-10, RS485, and Yoctopuce devices and exit")
    p.add_argument("--device-id", default="SPC", metavar="ID")
    p.add_argument("--window", type=float, default=600, metavar="SECS")
    p.add_argument("--ymin", type=float, default=0, metavar="MM",
                   help="Displacement Y-axis min")
    p.add_argument("--ymax", type=float, default=30, metavar="MM",
                   help="Displacement Y-axis max")
    p.add_argument("--hz", type=float, default=30, metavar="HZ")
    p.add_argument("--ch-name", default="SPC", metavar="NAME", help="Displacement legend")
    p.add_argument("--tag", default="", help="Free-text label for this cycle")
    p.add_argument("--force-type", choices=("mark10", "loadcell"), default="mark10",
                   help="Force sensor backend")
    p.add_argument("--force-baud", type=int, default=None,
                   help="Serial baud (default: 115200 Mark-10, 9600 load cell)")
    p.add_argument("--force-id", default="M5", metavar="ID")
    p.add_argument("--force-ch-name", default="Force", metavar="NAME")
    p.add_argument(
        "--loadcell-range",
        metavar="CAPACITY",
        default="100kg",
        help=f"Load cell full scale when --force-type loadcell ({range_choices()})",
    )
    p.add_argument("--loadcell-addr", type=int, default=1, metavar="ID",
                   help="Modbus slave address (1–99)")
    p.add_argument("--loadcell-decimals", type=int, default=None, metavar="N",
                   help="Override decimal places from transmitter (auto if omitted)")
    p.add_argument("--loadcell-debug", action="store_true",
                   help="Print raw Modbus registers on stderr")
    p.add_argument("--loadcell-cal", default=None, metavar="PATH",
                   help=f"Raw-ADC calibration file (default: {default_cal_path()}). "
                        "Create with spc-loadcell-cal.")
    p.add_argument("--loadcell-no-cal", action="store_true",
                   help="Ignore saved calibration; use the transmitter's scaled "
                        "weight register (whole-kg only on 0-decimal transmitters)")
    p.add_argument("--fmin", type=float, default=None, metavar="N",
                   help="Force Y-axis min (default: 0 for load cell, -fmax for Mark-10)")
    p.add_argument("--fmax", type=float, default=None, metavar="N",
                   help=f"Force Y-axis max (default: {DEFAULT_LOADCELL_MAX_N:g} for load cell, "
                        f"M5-10 full scale for Mark-10)")
    p.add_argument("--tmin", type=float, default=0.0, metavar="C",
                   help="Temperature Y-axis min")
    p.add_argument("--tmax", type=float, default=800.0, metavar="C",
                   help="Temperature Y-axis max")
    p.add_argument("--temp-serial", action="append", default=None, metavar="SERIAL",
                   help="Pin Yocto-Thermocouple module serial(s); repeatable, "
                        "order sets TC numbering (default: all discovered "
                        "modules; with a pin, startup proceeds even if the "
                        "module is offline)")
    p.add_argument("--temp-ch1-name", default="TC1", metavar="NAME",
                   help="Thermocouple 1 legend")
    p.add_argument("--temp-ch2-name", default="TC2", metavar="NAME",
                   help="Thermocouple 2 legend")
    p.add_argument(
        "--data-file", default=default_log_path(), metavar="PATH",
        help=f"HDF5 log (default: {default_log_path()})",
    )
    args = p.parse_args()
    if not args.list_ports:
        if not args.mode:
            p.error("--mode is required (choose one or more of: "
                    + ", ".join(MODES) + ")")
        # Dedupe while keeping the user's axis order.
        args.mode = list(dict.fromkeys(args.mode))
        args.ports = resolve_ports(p, args.mode, args.port)
    return args


def _display_path(path: str) -> str:
    """Shorten a path under $HOME to ~/... for display."""
    home = os.path.expanduser("~")
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def format_startup_panel(rows: list[tuple[str, str]], footer: list[str],
                         ascii_only: bool = False) -> str:
    """Bordered two-section summary box printed once at startup.

    Fits the terminal: long values wrap onto continuation lines aligned
    under the value column.
    """
    tl, tr, bl, br, h, v, ml, mr = (
        ("+", "+", "+", "+", "-", "|", "+", "+") if ascii_only
        else ("╭", "╮", "╰", "╯", "─", "│", "├", "┤")
    )
    term_w = shutil.get_terminal_size((100, 24)).columns
    max_content = max(40, min(96, term_w - 6))
    label_w = max(len(label) for label, _ in rows)
    body: list[str] = []
    for label, value in rows:
        parts = textwrap.wrap(value, max_content - label_w - 3) or [""]
        body.append(f"{label:<{label_w}}   {parts[0]}")
        body.extend(f"{'':<{label_w}}   {cont}" for cont in parts[1:])
    wrapped_footer = [line for f in footer
                      for line in (textwrap.wrap(f, max_content) or [""])]
    footer = wrapped_footer
    width = max(len(line) for line in body + footer)

    def edge(left: str, right: str) -> str:
        return left + h * (width + 4) + right

    def line(s: str) -> str:
        return f"{v}  {s:<{width}}  {v}"

    return "\n".join([
        edge(tl, tr),
        *(line(b) for b in body),
        edge(ml, mr),
        *(line(f) for f in footer),
        edge(bl, br),
    ])


def poll_channel(ch: SPCChannel) -> float:
    try:
        return to_mm(ch.read())
    except OSError as exc:
        print(f"  Read error on {ch.port}: {exc}", file=sys.stderr)
        return float("nan")


def poll_temperature(tch: TemperatureChannel) -> tuple[float, float]:
    try:
        r1, r2 = tch.read()
    except OSError as exc:
        print(f"  Temperature read error on {tch.port}: {exc}", file=sys.stderr)
        return float("nan"), float("nan")
    return to_c(r1), to_c(r2)


def main():
    args = parse_args()

    if args.list_ports:
        print(format_device_list())
        print()
        print(format_force_device_list())
        print()
        print(format_loadcell_device_list())
        print()
        print(format_temperature_device_list())
        return

    mode_names: list[str] = args.mode
    ports: dict[str, str | None] = args.ports
    # Startup summary rows, printed as one bordered panel once setup is done.
    panel_rows: list[tuple[str, str]] = []

    if args.force_baud is None:
        args.force_baud = 9600 if args.force_type == "loadcell" else 115200

    ch: SPCChannel | None = None
    if "displacement" in mode_names:
        port = ports["displacement"] or auto_port()
        if not port:
            print(format_device_list(), file=sys.stderr)
            sys.exit(
                "No Mitutoyo device found.\n"
                "Connect USB-ITN (or give --port displacement=PATH); on Linux "
                "install udev rules and replug."
            )
        try:
            ch = open_channel(port, baud=args.baud)
        except OSError as exc:
            sys.exit(
                f"{exc}\n"
                "If you see 'Access denied', install udev rules and replug:\n"
                "  sudo spc-reader-install-udev\n"
                "  sudo udevadm control --reload && sudo udevadm trigger"
            )
        panel_rows.append(("Displacement", port))

    force_ch: ForceChannel | None = None
    loadcell_range_kg: float | None = None
    raw_cal: RawCalibration | None = None
    force_port: str | None = None
    # Load cell mode supports hot-plug: the channel may be absent at startup
    # (or unplugged mid-session) and is (re)connected by polling for the port.
    force_expected = False
    if "force" in mode_names:
        if args.force_type == "loadcell":
            force_port = ports["force"]
            if not force_port:
                candidates = list_rs485_ports()
                if len(candidates) == 1:
                    force_port = candidates[0][0]
                    print(f"  RS485 adapter auto-detected: {force_port}")
                else:
                    print(format_loadcell_device_list(), file=sys.stderr)
                    sys.exit(
                        "Load cell mode needs a port (--port force=PATH): "
                        + ("no RS485 adapter candidates found."
                           if not candidates
                           else "multiple candidates — pick one.")
                    )
            force_port = normalize_port(force_port)
            try:
                loadcell_range_kg = parse_loadcell_range(args.loadcell_range)
            except ValueError as exc:
                sys.exit(str(exc))
            if not args.loadcell_no_cal:
                raw_cal = load_calibration(force_port, path=args.loadcell_cal)
            if raw_cal is not None:
                panel_rows.append((
                    "Calibration",
                    f"raw ADC — zero {raw_cal.zero_counts:.0f} counts, "
                    f"{raw_cal.counts_per_kg:.1f} counts/kg",
                ))
            else:
                panel_rows.append((
                    "Calibration",
                    "none — transmitter's scaled register (whole-kg only); "
                    "run spc-loadcell-cal",
                ))
            if args.force_id == "M5":
                args.force_id = "LC"
            force_expected = True
            lc_desc = (f"RS485 load cell, {loadcell_range_kg:g} kg FS, "
                       f"addr {args.loadcell_addr}")
            try:
                force_ch = open_loadcell_channel(
                    force_port,
                    range_kg=loadcell_range_kg,
                    baud=args.force_baud,
                    slave_id=args.loadcell_addr,
                    decimals=args.loadcell_decimals,
                    debug=args.loadcell_debug,
                    raw_cal=raw_cal,
                )
                panel_rows.append(("Force", f"{force_port}  ({lc_desc})"))
            except OSError as exc:
                print(f"  Load cell not connected: {exc}", file=sys.stderr)
                panel_rows.append((
                    "Force",
                    f"waiting for {force_port} — plug in anytime  ({lc_desc})",
                ))
        else:
            force_port = ports["force"] or auto_force_port()
            if not force_port:
                print(format_force_device_list(), file=sys.stderr)
                sys.exit(
                    "No Mark-10 found. Connect the gauge "
                    "(or give --port force=PATH / --force-type loadcell)."
                )
            force_port = normalize_port(force_port)
            try:
                force_ch = open_force_channel(force_port, baud=args.force_baud)
                panel_rows.append(("Force", f"{force_port}  (Mark-10)"))
            except OSError as exc:
                sys.exit(f"Could not open Mark-10 on {force_port!r}: {exc}")

    temp_chs: list[TemperatureChannel] = []
    if "temperature" in mode_names:
        try:
            temp_chs = open_temperature_channels(
                ports["temperature"] or "usb", serials=args.temp_serial
            )
        except OSError as exc:
            print(format_temperature_device_list(), file=sys.stderr)
            sys.exit(str(exc))
        for tch in temp_chs:
            panel_rows.append(
                ("Temperature", f"Yocto-Thermocouple {tch.serial}  ({tch.port})")
            )

    # In load cell mode the force channel counts as present even while the
    # adapter is unplugged — datasets/axes are created up front and samples
    # start flowing the moment the device (re)appears.
    is_loadcell = loadcell_range_kg is not None
    fmax = args.fmax
    if fmax is None:
        fmax = DEFAULT_LOADCELL_MAX_N if is_loadcell else DEFAULT_FORCE_MAX_N
    if args.fmin is not None:
        fmin = args.fmin
    else:
        # Load cell reads positive-only; Mark-10 is bipolar (tension/compression).
        fmin = 0.0 if is_loadcell else -fmax

    max_points = int(args.window * args.hz * 1.1)
    times = collections.deque(maxlen=max_points)

    def make_series(key: str, label: str, color: str, decimals: int) -> Series:
        return Series(key, label, color, decimals,
                      vals=collections.deque(maxlen=max_points))

    # One Mode per --mode entry, in the user's order: first mode owns the left
    # y-axis, each further mode adds its own axis on the right.
    modes: list[Mode] = []
    log_columns: list[tuple[str, str]] = []
    log_attrs: dict[str, object] = {}
    title_parts: list[str] = []
    for name in mode_names:
        if name == "displacement":
            modes.append(Mode(name, UNIT_MM, DISP_COLOR, (args.ymin, args.ymax), [
                make_series(DISPLACEMENT_DATASET, args.ch_name, DISP_COLOR, 4),
            ]))
            log_columns.append((DISPLACEMENT_DATASET, "f4"))
            log_attrs["serial"] = args.device_id
            log_attrs["units"] = UNIT_MM
            log_attrs["channel_name"] = args.ch_name
            title_parts.append("SPC displacement")
        elif name == "force":
            modes.append(Mode(name, UNIT_N, FORCE_COLOR, (fmin, fmax), [
                make_series(FORCE_DATASET, args.force_ch_name, FORCE_COLOR, 4),
            ]))
            log_columns.append((FORCE_DATASET, "f4"))
            log_attrs["force_serial"] = args.force_id
            log_attrs["force_units"] = UNIT_N
            log_attrs["force_channel_name"] = args.force_ch_name
            if is_loadcell:
                log_attrs["force_capacity_n"] = capacity_n(loadcell_range_kg)
                # Raw counts are logged but not plotted (no Series).
                log_columns.append((FORCE_COUNTS_DATASET, "f8"))
                log_attrs["force_counts_source"] = "raw ADC (registers 40015/16)"
                if raw_cal is not None:
                    log_attrs["force_cal_source"] = "raw-adc"
                    log_attrs["force_cal_zero_counts"] = raw_cal.zero_counts
                    log_attrs["force_cal_counts_per_kg"] = raw_cal.counts_per_kg
                else:
                    log_attrs["force_cal_source"] = "scaled-register"
            title_parts.append(
                "load cell force" if is_loadcell else "Mark-10 force"
            )
        else:  # temperature
            # TC numbering is continuous across modules: module i (0-based)
            # contributes TC 2i+1 and 2i+2 on the shared temperature axis.
            temp_series: list[Series] = []
            log_attrs["temp_serial"] = ",".join(t.serial for t in temp_chs)
            log_attrs["temp_units"] = "C"
            k = 0
            for tch in temp_chs:
                for _fn in TEMP_FUNCTIONS:
                    k += 1
                    key = temp_dataset(k)
                    label = {1: args.temp_ch1_name, 2: args.temp_ch2_name}.get(
                        k, f"TC{k}"
                    )
                    color = TEMP_COLORS[(k - 1) % len(TEMP_COLORS)]
                    temp_series.append(make_series(key, label, color, 1))
                    log_columns.append((key, "f4"))
                    log_attrs[f"temp_ch{k}_name"] = label
                    log_attrs[f"temp_ch{k}_serial"] = tch.serial
            modes.append(Mode(name, UNIT_C, temp_series[0].color,
                              (args.tmin, args.tmax), temp_series))
            title_parts.append("thermocouple temperature")

    all_series = [(m, s) for m in modes for s in m.series]

    data_file = resolve_data_path(args.data_file)
    logger = DataLogger(data_file, args.hz, args.tag,
                        columns=log_columns, attrs=log_attrs)
    panel_rows.append(("Log file", _display_path(data_file)))
    panel_rows.append(("Cycle", logger.cycle_name
                       + (f"   tag: {args.tag}" if args.tag else "")))

    title = " + ".join(title_parts)
    title = title[0].upper() + title[1:]

    # "s" is our save-metadata key; drop it from matplotlib's save-figure
    # shortcut (ctrl+s still opens the save dialog). "q" is handled by our own
    # quit path in on_key (cmd+w / ctrl+w still work via the keymap).
    plt.rcParams["keymap.save"] = [
        k for k in plt.rcParams["keymap.save"] if k != "s"
    ]
    plt.rcParams["keymap.quit"] = [
        k for k in plt.rcParams["keymap.quit"] if k != "q"
    ]

    plt.rcParams["font.sans-serif"] = ["Helvetica Neue", "Arial", "DejaVu Sans"]

    fig, ax_main = plt.subplots(num=title, figsize=(13, 6))
    fig.patch.set_facecolor(SURFACE)
    ax_main.set_facecolor(PANEL)

    # With several modes each y-axis is tinted to its series so they can be
    # told apart; a lone axis wears plain text color.
    multi = len(modes) > 1
    for i, mode in enumerate(modes):
        mode.ax = ax_main if i == 0 else ax_main.twinx()
        if i == 2:
            # A third axis also sits on the right; push its spine outward so
            # the two right-hand scales don't overprint.
            mode.ax.spines["right"].set_position(("outward", 55))
        for s in mode.series:
            s.line, = mode.ax.plot([], [], color=s.color, linewidth=2,
                                   label=s.label)
        mode.ax.set_ylim(*mode.ylim)
        ink = mode.axis_color if multi else INK
        mode.ax.set_ylabel(mode.unit, fontsize=15, color=ink)
        mode.ax.tick_params(axis="y", labelcolor=ink)

    for ax in {mode.ax for mode in modes}:
        ax.spines["top"].set_visible(False)
        for spine in ax.spines.values():
            spine.set_color(BORDER)
        ax.tick_params(color=BORDER, labelsize=13)
    ax_main.tick_params(axis="x", labelcolor=INK_MUTED)
    if not multi:
        ax_main.spines["right"].set_visible(False)

    ax_main.legend([s.line for _m, s in all_series],
                   [s.label for _m, s in all_series],
                   loc="upper right", fontsize=13, frameon=False, labelcolor=INK)

    ax_main.set_xlabel(f"Time before now  ({int(args.window)} s window)",
                       fontsize=13, color=INK_MUTED)

    # Relative-time x-axis: the newest sample sits at x=0 (right), older data to
    # the left. Fixed limits mean the axes background never changes, so the plot
    # can be blitted (only the line is redrawn). Absolute wall-clock time is still
    # shown in the readout and hover tooltips.
    X_LEAD_S = args.window * 0.02
    ax_main.set_xlim(-args.window, X_LEAD_S)

    def _rel_time_fmt(x, _pos) -> str:
        s = int(round(-x))
        if s <= 0:
            return "now"
        m, sec = divmod(s, 60)
        return f"-{m}:{sec:02d}" if m else f"-{sec}s"

    ax_main.set_axisbelow(True)
    ax_main.yaxis.grid(True, color=GRID, linewidth=0.9)
    ax_main.xaxis.grid(False)
    ax_main.xaxis.set_major_formatter(FuncFormatter(_rel_time_fmt))

    readout = ax_main.text(
        0.01, 0.97, "", transform=ax_main.transAxes,
        fontsize=14, verticalalignment="top", family="monospace", color=INK,
        bbox=dict(boxstyle="round,pad=0.45", facecolor="white",
                  alpha=0.92, edgecolor=BORDER),
    )

    status_text = ax_main.text(
        0.5, 0.97, "", transform=ax_main.transAxes,
        fontsize=14, fontweight="bold", verticalalignment="top",
        horizontalalignment="center", color=ALERT, visible=False,
        bbox=dict(boxstyle="round,pad=0.5", facecolor="#fdecec",
                  alpha=0.95, edgecolor=ALERT),
    )

    wait_text = ax_main.text(
        0.5, 0.86, "", transform=ax_main.transAxes,
        fontsize=13, fontweight="bold", verticalalignment="top",
        horizontalalignment="center", color=WARN, visible=False,
        bbox=dict(boxstyle="round,pad=0.5", facecolor="#fef6ee",
                  alpha=0.95, edgecolor=WARN),
    )
    if force_expected:
        wait_text.set_text(
            f"Load cell not connected — waiting for {force_port}"
        )
        wait_text.set_visible(force_ch is None)

    hover_color = modes[0].series[0].color
    hover_dot, = ax_main.plot([], [], "o", color=hover_color, markersize=8, zorder=5,
                              markeredgecolor="white", markeredgewidth=1.5)
    hover_ann = ax_main.annotate(
        "", xy=(0, 0), xytext=(8, 8), textcoords="offset points",
        fontsize=13, color=INK,
        bbox=dict(boxstyle="round,pad=0.45", facecolor="white",
                  edgecolor=BORDER, alpha=0.95),
        visible=False,
    )

    # --- Blitting: only the line/readout/hover artists are redrawn each frame,
    # over a cached static background. Mark them "animated" so full draws skip
    # them (keeping the cached background free of stale line data).
    dyn_artists = tuple(s.line for _m, s in all_series) + (
        readout, status_text, wait_text, hover_dot, hover_ann,
    )
    for _a in dyn_artists:
        _a.set_animated(True)

    render = {"bg": None}
    # Set on close_event before teardown. Callbacks queued behind the close
    # (timer ticks, motion events) must not touch the canvas: on Windows/Tk
    # matplotlib swaps in a plain FigureCanvasBase during window destruction,
    # which lacks the Agg blitting API (restore_region/copy_from_bbox), and
    # drawing on the dead Tk widget raises.
    closing = {"on": False}

    def _can_draw() -> bool:
        return not closing["on"] and hasattr(fig.canvas, "restore_region")

    def _draw_dynamic() -> None:
        for a in dyn_artists:
            if a.get_visible():
                a.axes.draw_artist(a)

    def on_draw(_event=None) -> None:
        # After any full draw (startup, resize, expose, widget typing) re-cache the
        # background and immediately repaint the dynamic artists on top of it.
        if not _can_draw():
            return
        render["bg"] = fig.canvas.copy_from_bbox(fig.bbox)
        _draw_dynamic()
        fig.canvas.blit(fig.bbox)

    fig.canvas.mpl_connect("draw_event", on_draw)

    def blit_frame() -> None:
        if not _can_draw():
            return
        if render["bg"] is None:
            fig.canvas.draw()   # triggers on_draw, which caches bg and blits
            return
        fig.canvas.restore_region(render["bg"])
        _draw_dynamic()
        fig.canvas.blit(fig.bbox)

    def full_refresh() -> None:
        # Force a full redraw (picks up title/axis/widget changes), then blit.
        if not _can_draw():
            return
        fig.canvas.draw()

    # live["x"] = relative seconds (plot coords), live["t_unix"] = absolute time
    # for tooltips, plus one array per series key. Use len(), never truthiness.
    live = {"x": np.empty(0), "t_unix": np.empty(0)}
    for _m, s in all_series:
        live[s.key] = np.empty(0)

    def on_hover(event):
        if closing["on"]:
            return
        hover_axes = tuple(m.ax for m in modes)
        if event.inaxes not in hover_axes or len(live["x"]) == 0:
            hover_dot.set_data([], [])
            hover_ann.set_visible(False)
            blit_frame()
            return

        x_arr = live["x"]
        # The dot/annotation anchor to the first series (in mode order) with data.
        anchor = next(((m, s) for m, s in all_series if len(live[s.key])), None)
        if anchor is None:
            hover_dot.set_data([], [])
            hover_ann.set_visible(False)
            blit_frame()
            return
        hover_ax = anchor[0].ax
        y_arr = live[anchor[1].key]

        x_display = hover_ax.transData.transform(np.column_stack([x_arr, y_arr]))[:, 0]
        idx = int(np.argmin(np.abs(x_display - event.x)))

        tx = x_arr[idx]
        ts = datetime.datetime.fromtimestamp(
            live["t_unix"][idx]
        ).strftime("%H:%M:%S.%f")[:-3]
        lines_txt = [ts]
        for m, s in all_series:
            arr = live[s.key]
            if not len(arr):
                continue
            vy_s = arr[idx] if idx < len(arr) else float("nan")
            v_str = f"{vy_s:.{s.decimals}f} {m.unit}" if not np.isnan(vy_s) else "No data"
            lines_txt.append(f"{s.label}: {v_str}")

        vy = y_arr[idx]
        if not np.isnan(vy):
            hover_dot.set_data([tx], [vy])
        else:
            hover_dot.set_data([], [])
        hover_ann.xy = (tx, vy)
        hover_ann.set_text("\n".join(lines_txt))
        hover_ann.set_visible(True)

        ax_ext = hover_ax.get_window_extent()
        x_frac = (x_display[idx] - ax_ext.x0) / ax_ext.width
        x_off = -8 if x_frac > 0.5 else 8
        hover_ann.xyann = (x_off, 8)
        hover_ann.set_ha("right" if x_off < 0 else "left")
        blit_frame()

    fig.canvas.mpl_connect("motion_notify_event", on_hover)

    def reset_display() -> None:
        times.clear()
        live["x"] = np.empty(0)
        live["t_unix"] = np.empty(0)
        for _m, s in all_series:
            s.vals.clear()
            s.line.set_data([], [])
            live[s.key] = np.empty(0)
        hover_dot.set_data([], [])
        hover_ann.set_visible(False)
        readout.set_text("")

    running = {"on": True}
    label_box = {"widget": None, "saved": "", "indicator": None}
    # The sampler thread swaps the force channel in/out on hot-plug; the GUI
    # timer only reads it (to sync the "waiting" banner).
    force = {"ch": force_ch}
    conn_shown = {"waiting": force_expected and force_ch is None}
    fig._spc_force = force            # exposed for tests
    fig._spc_wait_text = wait_text    # exposed for tests

    # Acquisition runs on a background thread and hands samples to the GUI via
    # this queue as (t, {dataset_key: value}) so blocking reads never freeze
    # inputs/redraws.
    sample_q: "queue.Queue[tuple[float, dict[str, float | None]]]" = queue.Queue()
    stop_evt = threading.Event()

    def drain_queue() -> None:
        while True:
            try:
                sample_q.get_nowait()
            except queue.Empty:
                return

    def update_dirty_indicator() -> None:
        ind = label_box["indicator"]
        w = label_box["widget"]
        if ind is None or w is None:
            return
        ind.set_visible(w.text.strip() != label_box["saved"])

    def save_label_to_current() -> None:
        w = label_box["widget"]
        if w is not None:
            logger.set_label(w.text)
            label_box["saved"] = logger.label
            update_dirty_indicator()

    def on_key(event):
        # Ignore keys (incl. "t"/"s"/"q") while the user is typing in the label field.
        w = label_box["widget"]
        if w is not None and getattr(w, "capturekeystrokes", False):
            return
        if event.key == "q":
            print("  Quit (q).")
            plt.close(fig)
            return
        if event.key == "s":
            # Save the label to the current cycle; data is already on disk.
            save_label_to_current()
            logger.flush()
            print(f"  Metadata saved to [{logger.cycle_name}]: {logger.label!r}")
            full_refresh()
            return
        if event.key != "t":
            return
        if running["on"]:
            # Stop: finalize the current run's data on disk, freeze plot.
            # The label is NOT auto-saved — only "s" saves it.
            running["on"] = False
            logger.flush()
            status_text.set_text("STOPPED — press t to start a new run")
            status_text.set_visible(True)
            set_status_cell(False)
            print("  Stopped.  (log flushed; press t to start a NEW run)")
        else:
            # Start: begin a fresh run (never resume the stopped one).
            reset_display()
            drain_queue()             # discard anything sampled during the stop
            grp = logger.new_cycle()
            running["on"] = True
            status_text.set_visible(False)
            set_status_cell(True)
            # The label text carries over but the new cycle starts unlabeled,
            # so it shows as unsaved until the user presses "s" (or Enter).
            label_box["saved"] = ""
            update_dirty_indicator()
            refresh_cycle_cell()
            print(f"  New run → [{grp}]  (recording)")
        full_refresh()

    fig.canvas.mpl_connect("key_press_event", on_key)
    panel_footer = [
        "t  stop/start run      s  save label      q  quit",
        "Type a run note in the Label box; press s to save it to the cycle",
    ]
    panel = format_startup_panel(panel_rows, panel_footer)
    try:
        print(f"\n{panel}\n")
    except UnicodeEncodeError:  # redirected legacy-codepage console
        print(f"\n{format_startup_panel(panel_rows, panel_footer, ascii_only=True)}\n")

    GUI_INTERVAL_MS = 25  # ~40 fps; blitting keeps each frame cheap
    period = 1.0 / args.hz

    def connect_force() -> ForceChannel | None:
        """Try to (re)open the load cell; None while it is absent/unresponsive."""
        if not port_present(force_port):
            return None
        try:
            fch = open_loadcell_channel(
                force_port,
                range_kg=loadcell_range_kg,
                baud=args.force_baud,
                slave_id=args.loadcell_addr,
                decimals=args.loadcell_decimals,
                debug=args.loadcell_debug,
                raw_cal=raw_cal,
            )
        except OSError:
            return None
        # The adapter enumerates before the transmitter answers Modbus; only
        # claim "connected" once a real reading comes back.
        try:
            reading = fch.read()
        except OSError:
            reading = None
        if reading is None:
            try:
                fch.close()
            except OSError:
                pass
            return None
        return fch

    # Whether any channel besides the hot-pluggable load cell produces samples;
    # with none, the sampler idles instead of logging all-NaN rows while the
    # adapter is absent.
    other_readers = ch is not None or bool(temp_chs)
    # Dataset keys per temperature channel, in module order (TC 2i+1, 2i+2).
    temp_keys = [(temp_dataset(2 * i + 1), temp_dataset(2 * i + 2))
                 for i in range(len(temp_chs))]

    def sampler_loop() -> None:
        """Read the (slow, blocking) channels off the GUI thread."""
        next_t = time.monotonic()
        last_probe = -FORCE_RECONNECT_POLL_S
        while not stop_evt.is_set():
            if not running["on"]:
                stop_evt.wait(0.05)
                next_t = time.monotonic()
                continue

            fch = force["ch"]
            if force_expected and fch is None:
                now = time.monotonic()
                if now - last_probe >= FORCE_RECONNECT_POLL_S:
                    last_probe = now
                    fch = connect_force()
                    if fch is not None:
                        force["ch"] = fch
                        print(f"  Load cell connected on {force_port}")
                if fch is None and not other_readers:
                    stop_evt.wait(0.1)
                    next_t = time.monotonic()
                    continue

            values: dict[str, float | None] = {}
            if ch is not None:
                values[DISPLACEMENT_DATASET] = poll_channel(ch)
            for tch, (key1, key2) in zip(temp_chs, temp_keys):
                values[key1], values[key2] = poll_temperature(tch)
            if fch is not None:
                reading = None
                read_exc: OSError | None = None
                try:
                    reading = fch.read()
                except OSError as exc:
                    read_exc = exc
                if (force_expected and reading is None
                        and not port_present(fch.port)):
                    # Port node vanished: the adapter was unplugged. Drop the
                    # channel and fall back to waiting for it to reappear.
                    try:
                        fch.close()
                    except OSError:
                        pass
                    force["ch"] = None
                    last_probe = time.monotonic()
                    print(
                        f"  Load cell unplugged — waiting for {force_port} "
                        "to reappear",
                        file=sys.stderr,
                    )
                    if not other_readers:
                        next_t = time.monotonic()
                        continue  # nothing else to record this tick
                else:
                    if read_exc is not None:
                        print(f"  Force read error on {fch.port}: {read_exc}",
                              file=sys.stderr)
                    values[FORCE_DATASET] = to_n(reading)
                    values[FORCE_COUNTS_DATASET] = getattr(fch, "last_counts", None)
            sample_q.put((time.time(), values))
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                stop_evt.wait(delay)   # interruptible sleep; wakes on shutdown
            else:
                next_t = time.monotonic()  # fell behind (I/O slower than 1/hz)

    sampler = threading.Thread(target=sampler_loop, name="spc-sampler", daemon=True)

    def fmt(v: float, decimals: int, unit: str) -> str:
        if np.isnan(v):
            return "No data"
        return f"{v:.{decimals}f} {unit}"

    def on_timer() -> None:
        if closing["on"]:
            return  # a queued tick can still fire after the window closes
        # Keep the "waiting for load cell" banner in sync with the sampler
        # thread's hot-plug state (even while a run is stopped).
        if force_expected:
            waiting = force["ch"] is None
            if waiting != conn_shown["waiting"]:
                conn_shown["waiting"] = waiting
                wait_text.set_visible(waiting)
                blit_frame()

        if not running["on"]:
            return  # stopped: frozen frame stays on screen; consume nothing

        # Drain everything the sampler produced since the last frame (cheap; no I/O).
        last_t_unix = None
        while True:
            try:
                t_unix, values = sample_q.get_nowait()
            except queue.Empty:
                break
            last_t_unix = t_unix
            times.append(t_unix)   # store absolute unix seconds
            for _m, s in all_series:
                v = values.get(s.key)
                s.vals.append(v if v is not None else float("nan"))
            logger.append(t_unix, values)

        if last_t_unix is None or len(times) < 2:
            return

        # Decimate to at most MAX_DRAW_POINTS for drawing (keeping the newest
        # sample), so a full window doesn't cost thousands of line segments.
        n = len(times)
        if n > MAX_DRAW_POINTS:
            step = n // MAX_DRAW_POINTS + 1
            idx = np.arange(0, n, step)
            if idx[-1] != n - 1:
                idx = np.append(idx, n - 1)
        else:
            idx = None

        def thin(deq) -> np.ndarray:
            arr = np.fromiter(deq, dtype=float, count=len(deq))
            return arr if idx is None else arr[idx]

        # x is seconds relative to "now" (<= 0); the axis limits are fixed, so the
        # background never changes and the frame can be blitted.
        t_unix_arr = thin(times)
        x_rel = t_unix_arr - time.time()
        live["x"] = x_rel
        live["t_unix"] = t_unix_arr
        for _m, s in all_series:
            arr = thin(s.vals)
            s.line.set_data(x_rel, arr)
            live[s.key] = arr

        t_last = datetime.datetime.fromtimestamp(last_t_unix)
        readout_lines = []
        for m, s in all_series:
            if s.vals:
                readout_lines.append(
                    f"{s.label}: {fmt(s.vals[-1], s.decimals, m.unit)}"
                )
        readout_lines.append(t_last.strftime("%H:%M:%S.%f")[:-3])
        readout.set_text("\n".join(readout_lines))
        blit_frame()

    timer = fig.canvas.new_timer(interval=GUI_INTERVAL_MS)
    timer.add_callback(on_timer)
    fig._spc_timer = timer  # keep a reference so the timer stays alive

    fig.tight_layout(rect=(0, 0, 1, 0.862))
    if len(modes) > 2:
        # Leave room for the third axis's outward-offset spine; must happen
        # before main_pos is read so the info bar spans the adjusted width.
        fig.subplots_adjust(right=0.90)

    # Top info bar: one panel spanning the plot width, split into cells —
    # Cycle | Label (text entry) | Shortcuts | Status. Fieldset style: each
    # cell's caption sits centered ON the top border, breaking the line.
    main_pos = ax_main.get_position()
    BAR_H = 0.10
    bar_y0 = 0.978 - BAR_H
    ax_bar = fig.add_axes((main_pos.x0, bar_y0, main_pos.width, BAR_H))
    ax_bar.set_facecolor(PANEL)
    ax_bar.set_xticks([])
    ax_bar.set_yticks([])
    ax_bar.set_navigate(False)
    ax_bar.set_xlim(0, 1)
    ax_bar.set_ylim(0, 1)
    for spine in ax_bar.spines.values():
        spine.set_color(BORDER)

    # Cell boundaries, as fractions of the bar width. Cycle, Shortcuts and
    # Status hug their contents; the Label cell absorbs the rest.
    bar_px_w = main_pos.width * fig.get_figwidth() * fig.dpi
    PAD = 10 / bar_px_w          # ~10 px cell inset, in bar coords
    CONTENT_Y = 0.5

    # Provisional boundaries; layout_bar() below re-derives them from the
    # rendered contents (in pixels), so the Cycle/Shortcuts/Status cells never
    # get crushed on a small window — only the Label cell gives up space.
    x_label = 0.07               # right edge of the Cycle cell
    x_status = 1.0 - 0.085
    x_keys = x_status - 0.22
    dividers = [ax_bar.axvline(xb, color=BORDER, linewidth=1)
                for xb in (x_label, x_keys, x_status)]

    bar_top = bar_y0 + BAR_H
    captions = {}

    def cell_caption(x0: float, label: str) -> None:
        # Left-justified on the cell's top border; the surface-colored bbox
        # breaks the border line (fieldset style). Figure-level text so it
        # draws above the label-entry axes, which underlaps its caption.
        captions[label] = fig.text(
            main_pos.x0 + (x0 + PAD) * main_pos.width, bar_top, label,
            fontsize=10, color=INK_MUTED, ha="left", va="center",
            bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1.5),
        )

    cell_caption(0.0, "Cycle")
    cell_caption(x_label, "Label")
    cell_caption(x_keys, "Shortcuts")
    cell_caption(x_status, "Status")

    def cycle_display() -> str:
        num = logger.cycle_name
        return f"{num} — {args.tag}" if args.tag else num

    cycle_num_text = ax_bar.text(
        PAD, CONTENT_Y, cycle_display(),
        fontsize=13, fontweight="bold", color=INK, va="center",
    )

    def refresh_cycle_cell() -> None:
        cycle_num_text.set_text(cycle_display())

    keys_text = ax_bar.text(
        x_keys + PAD, CONTENT_Y, "t  start/stop     s  save label     q  quit",
        fontsize=10.5, color=INK, va="center",
    )

    status_val = ax_bar.text(
        x_status + PAD, CONTENT_Y, "● REC",
        fontsize=11.5, fontweight="bold", color=REC_GREEN, va="center",
    )

    def set_status_cell(recording: bool) -> None:
        if recording:
            status_val.set_text("● REC")
            status_val.set_color(REC_GREEN)
        else:
            status_val.set_text("STOPPED")
            status_val.set_color(ALERT)

    # Label entry: the whole cell is the input (widgets need their own axes).
    # Borderless — the bar's own border and dividers frame it; hovering
    # highlights the cell so it reads as an active input element.
    fig_px_w = fig.get_figwidth() * fig.dpi
    fig_px_h = fig.get_figheight() * fig.dpi
    eps_x, eps_y = 2 / fig_px_w, 2 / fig_px_h   # keep border/dividers visible
    lx0 = main_pos.x0 + x_label * main_pos.width + eps_x
    lx1 = main_pos.x0 + x_keys * main_pos.width - eps_x
    ax_label = fig.add_axes((lx0, bar_y0 + eps_y, lx1 - lx0, BAR_H - 2 * eps_y))
    textbox = EditableTextBox(ax_label, "", initial="",
                              textalignment="left", color=PANEL, hovercolor="#f5f5f4")
    textbox.text_disp.set_fontsize(12.5)
    textbox.text_disp.set_color(INK)
    # Start the entry text at the same inset as the cell captions.
    textbox.text_disp.set_position((10 / ((lx1 - lx0) * fig_px_w), 0.5))
    for spine in ax_label.spines.values():
        spine.set_visible(False)
    label_box["widget"] = textbox
    fig._spc_label_box = textbox  # keep a reference so the widget stays alive

    # Sits on the top border at the right end of the Label cell, breaking the
    # line the same way the captions do.
    unsaved_ind = fig.text(
        main_pos.x0 + (x_keys - PAD) * main_pos.width, bar_top,
        "* unsaved — press s",
        color=ALERT, fontsize=9, ha="right", va="center",
        bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1.5),
    )
    unsaved_ind.set_visible(False)
    label_box["indicator"] = unsaved_ind

    def layout_bar(_event=None) -> None:
        # Pin Cycle/Shortcuts/Status to their rendered content widths so a
        # small window can't crush them; the Label cell takes what remains.
        if closing["on"]:
            return
        try:
            def frac_w(artist) -> float:  # rendered width as a bar fraction
                return (artist.get_window_extent().width
                        / (main_pos.width * fig.bbox.width))

            cur = status_val.get_text()
            stat_w = 0.0
            for s in ("● REC", "STOPPED"):  # size for the wider state
                status_val.set_text(s)
                stat_w = max(stat_w, frac_w(status_val))
            status_val.set_text(cur)

            fig_w = fig.get_figwidth() * fig.dpi
            bar_w = main_pos.width * fig_w
            pad = 10.0 / bar_w           # ~10 px, as a bar fraction

            def cell_w(content: float, cap: str) -> float:
                return max(content, frac_w(captions[cap])) + 2 * pad

            x_lab = cell_w(frac_w(cycle_num_text), "Cycle")
            x_stat = 1.0 - cell_w(stat_w, "Status")
            x_key = x_stat - cell_w(frac_w(keys_text), "Shortcuts")
            x_key = max(x_key, x_lab + 0.02)   # Label keeps at least a sliver
        except RuntimeError:
            return  # no renderer yet; retried after the first draw/resize

        for line, xv in zip(dividers, (x_lab, x_key, x_stat)):
            line.set_xdata([xv, xv])

        def fx(bar_x: float) -> float:   # bar fraction → figure fraction
            return main_pos.x0 + bar_x * main_pos.width

        captions["Cycle"].set_position((fx(pad), bar_top))
        captions["Label"].set_position((fx(x_lab + pad), bar_top))
        captions["Shortcuts"].set_position((fx(x_key + pad), bar_top))
        captions["Status"].set_position((fx(x_stat + pad), bar_top))
        cycle_num_text.set_position((pad, CONTENT_Y))
        keys_text.set_position((x_key + pad, CONTENT_Y))
        status_val.set_position((x_stat + pad, CONTENT_Y))
        unsaved_ind.set_position((fx(x_key - pad), bar_top))

        eps_x = 2 / fig_w
        eps_y = 2 / (fig.get_figheight() * fig.dpi)
        lx0, lx1 = fx(x_lab) + eps_x, fx(x_key) - eps_x
        ax_label.set_position((lx0, bar_y0 + eps_y, lx1 - lx0, BAR_H - 2 * eps_y))
        textbox.text_disp.set_position((10.0 / ((lx1 - lx0) * fig_w), 0.5))

    fig.canvas.mpl_connect("resize_event", layout_bar)

    # Saving is explicit ("s" key only). TextBox fires "submit" on ANY focus
    # loss (Enter or clicking away), so a submit handler must not save — it
    # would silently persist the label whenever the box loses focus.
    def on_label_change(_text: str) -> None:
        update_dirty_indicator()

    textbox.on_text_change(on_label_change)

    def on_close(_event) -> None:
        # Stop the timer and sampler the moment the window closes (q, cmd+w,
        # or the close button). An active timer can keep the macosx event loop
        # alive after the last window closes, leaving the process running with
        # the serial port open — killing it then wedges the FTDI driver until
        # the adapter is replugged (open() fails with termios EINVAL).
        closing["on"] = True   # queued callbacks must not touch the dying canvas
        timer.stop()
        stop_evt.set()

    fig.canvas.mpl_connect("close_event", on_close)

    # If the process dies by signal (closed terminal → SIGHUP, kill → SIGTERM,
    # Ctrl+Break on Windows → SIGBREAK), exit via SystemExit so the finally
    # block below still closes the port. Not every signal exists per platform.
    for _name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        _sig = getattr(signal, _name, None)
        if _sig is not None:
            signal.signal(_sig, lambda *_a: sys.exit(1))

    fig.canvas.draw()      # realize a renderer so the bar contents can be measured
    layout_bar()

    sampler.start()
    timer.start()
    try:
        plt.show()
    finally:
        timer.stop()
        stop_evt.set()
        sampler.join(timeout=5.0)
        if sampler.is_alive():
            print("  Warning: sampler thread still busy at exit.", file=sys.stderr)
        logger.close()
        for c in (ch, force["ch"], *temp_chs):
            if c is not None:
                try:
                    c.close()
                except OSError:
                    pass


if __name__ == "__main__":
    main()
