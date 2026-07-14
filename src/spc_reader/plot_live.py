#!/usr/bin/env python3
"""Real-time Mitutoyo displacement + Mark-10 force plotter and HDF5 logger."""

import argparse
import collections
import datetime
import os
import queue
import signal
import sys
import threading
import time

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
)

DISP_COLOR = "#eb6834"   # orange — displacement series
FORCE_COLOR = "#2a78d6"  # blue — force series
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


class DataLogger:
    """Buffers and flushes (time, displacement_mm[, force_n]) to HDF5."""

    FLUSH_EVERY = 25

    def __init__(
        self,
        filepath: str,
        device_id: str,
        hz: float,
        tag: str,
        ch_name: str,
        *,
        log_displacement: bool = True,
        log_force: bool = False,
        force_device_id: str = "",
        force_ch_name: str = "Force",
        force_capacity_n: float | None = None,
        log_counts: bool = False,
        force_cal: RawCalibration | None = None,
    ):
        if not log_displacement and not log_force:
            raise ValueError("DataLogger needs at least one channel")
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        self._path = filepath
        self._device_id = device_id
        self._hz = hz
        self._tag = tag
        self._ch_name = ch_name
        self._log_displacement = log_displacement
        self._log_force = log_force
        self._force_device_id = force_device_id
        self._force_ch_name = force_ch_name
        self._force_capacity_n = force_capacity_n
        self._log_counts = log_counts and log_force
        self._force_cal = force_cal
        self._buf_t: list[float] = []
        self._buf_mm: list[float] = []
        self._buf_n: list[float] = []
        self._buf_counts: list[float] = []
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
            if self._log_displacement:
                grp.attrs["serial"]       = self._device_id
                grp.attrs["units"]        = UNIT_MM
                grp.attrs["channel_name"] = self._ch_name
            if self._log_force:
                grp.attrs["force_serial"]       = self._force_device_id
                grp.attrs["force_units"]        = UNIT_N
                grp.attrs["force_channel_name"] = self._force_ch_name
                if self._force_capacity_n is not None:
                    grp.attrs["force_capacity_n"] = self._force_capacity_n
            if self._log_counts:
                grp.attrs["force_counts_source"] = "raw ADC (registers 40015/16)"
                if self._force_cal is not None:
                    grp.attrs["force_cal_source"]        = "raw-adc"
                    grp.attrs["force_cal_zero_counts"]   = self._force_cal.zero_counts
                    grp.attrs["force_cal_counts_per_kg"] = self._force_cal.counts_per_kg
                else:
                    grp.attrs["force_cal_source"] = "scaled-register"
            datasets: list[tuple[str, str]] = [("time", "f8")]
            if self._log_displacement:
                datasets.append((DISPLACEMENT_DATASET, "f4"))
            if self._log_force:
                datasets.append((FORCE_DATASET, "f4"))
            if self._log_counts:
                datasets.append((FORCE_COUNTS_DATASET, "f8"))
            for ds_name, dtype in datasets:
                grp.create_dataset(
                    ds_name, shape=(0,), maxshape=(None,),
                    dtype=dtype, chunks=(256,), compression="gzip",
                )
            self._grp_path = f"cycles/{grp_name}"
            self._cycle_name = grp_name
            self._label = ""

        tag_display = f"  tag={self._tag!r}" if self._tag else ""
        print(f"Logging → {self._path}  [{self._grp_path}]{tag_display}")

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

    def append(
        self,
        unix_t: float,
        displacement_mm: float | None = None,
        force_n: float | None = None,
        force_counts: float | None = None,
    ) -> None:
        self._buf_t.append(unix_t)
        if self._log_displacement:
            self._buf_mm.append(
                displacement_mm if displacement_mm is not None else float("nan")
            )
        if self._log_force:
            self._buf_n.append(force_n if force_n is not None else float("nan"))
        if self._log_counts:
            self._buf_counts.append(
                float(force_counts) if force_counts is not None else float("nan")
            )
        if len(self._buf_t) >= self.FLUSH_EVERY:
            self._flush()

    def flush(self) -> None:
        """Persist buffered samples of the current cycle without starting a new one."""
        self._flush()

    def close(self) -> None:
        self._flush()

    def _flush(self) -> None:
        if not self._buf_t:
            return
        t_data = self._buf_t[:]
        self._buf_t.clear()
        pairs: list[tuple[str, list]] = [("time", t_data)]
        if self._log_displacement:
            mm_data = self._buf_mm[:]
            self._buf_mm.clear()
            pairs.append((DISPLACEMENT_DATASET, mm_data))
        if self._log_force:
            n_data = self._buf_n[:]
            self._buf_n.clear()
            pairs.append((FORCE_DATASET, n_data))
        if self._log_counts:
            counts_data = self._buf_counts[:]
            self._buf_counts.clear()
            pairs.append((FORCE_COUNTS_DATASET, counts_data))
        with h5py.File(self._path, "a") as f:
            grp = f[self._grp_path]
            for ds_name, data in pairs:
                ds = grp[ds_name]
                n = len(ds)
                ds.resize(n + len(data), axis=0)
                ds[n:] = data


def parse_args():
    p = argparse.ArgumentParser(
        description="Mitutoyo SPC displacement and force logger (Mark-10 or RS485 load cell) with live plotting",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--port", metavar="PATH",
                   help="usb-itn, usb-itn:SERIAL, or /dev/tty* (default: auto)")
    p.add_argument("--no-displacement", action="store_true",
                   help="Force only (skip Mitutoyo displacement channel)")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--list-ports", action="store_true",
                   help="List Mitutoyo and Mark-10 devices and exit")
    p.add_argument("--device-id", default="SPC", metavar="ID")
    p.add_argument("--window", type=float, default=600, metavar="SECS")
    p.add_argument("--ymin", type=float, default=0, metavar="MM")
    p.add_argument("--ymax", type=float, default=30, metavar="MM")
    p.add_argument("--hz", type=float, default=30, metavar="HZ")
    p.add_argument("--ch-name", default="SPC", metavar="NAME", help="Displacement legend")
    p.add_argument("--tag", default="", help="Free-text label for this cycle")
    p.add_argument("--force-port", metavar="PATH",
                   help="Force sensor port: Mark-10 USB or RS485 adapter "
                        "(/dev/tty*, or COMx on Windows)")
    p.add_argument("--force-type", choices=("mark10", "loadcell"), default="mark10",
                   help="Force sensor backend")
    p.add_argument("--no-force", action="store_true",
                   help="Displacement only (skip force channel)")
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
    p.add_argument(
        "--data-file", default=default_log_path(), metavar="PATH",
        help=f"HDF5 log (default: {default_log_path()})",
    )
    return p.parse_args()


def poll_channel(ch: SPCChannel) -> float:
    try:
        return to_mm(ch.read())
    except OSError as exc:
        print(f"  Read error on {ch.port}: {exc}", file=sys.stderr)
        return float("nan")


def main():
    args = parse_args()

    if args.list_ports:
        print(format_device_list())
        print()
        print(format_force_device_list())
        print()
        print(format_loadcell_device_list())
        return

    if args.force_port:
        args.force_port = normalize_port(args.force_port)

    if args.force_baud is None:
        args.force_baud = 9600 if args.force_type == "loadcell" else 115200

    if args.no_displacement and args.no_force:
        sys.exit("Need at least one channel (omit --no-displacement or --no-force).")

    ch: SPCChannel | None = None
    if not args.no_displacement:
        port = args.port or auto_port()
        if not port:
            print(format_device_list(), file=sys.stderr)
            sys.exit(
                "No Mitutoyo device found.\n"
                "Use --no-displacement for force-only, or install udev rules and replug USB-ITN."
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
        print(f"  Displacement port: {port}")
    elif args.no_force:
        print("  Displacement only (--no-force).")
    else:
        print("  Force only (--no-displacement).")

    force_ch: ForceChannel | None = None
    loadcell_range_kg: float | None = None
    raw_cal: RawCalibration | None = None
    # Load cell mode supports hot-plug: the channel may be absent at startup
    # (or unplugged mid-session) and is (re)connected by polling for the port.
    force_expected = False
    if not args.no_force:
        if args.force_type == "loadcell":
            if not args.force_port:
                sys.exit(
                    "Load cell mode requires --force-port (RS485 USB adapter, "
                    "e.g. /dev/ttyUSB0, /dev/cu.usbserial-XXXX on macOS, "
                    "or COM5 on Windows).\n"
                    f"Ranges: {range_choices()}"
                )
            try:
                loadcell_range_kg = parse_loadcell_range(args.loadcell_range)
            except ValueError as exc:
                sys.exit(str(exc))
            if not args.loadcell_no_cal:
                raw_cal = load_calibration(args.force_port, path=args.loadcell_cal)
            if raw_cal is not None:
                print(
                    f"  Calibration: raw ADC, zero={raw_cal.zero_counts:.0f} "
                    f"counts, {raw_cal.counts_per_kg:.1f} counts/kg"
                )
            else:
                print(
                    "  Calibration: none — using transmitter's scaled register "
                    "(may be whole-kg only). Run spc-loadcell-cal for resolution.",
                    file=sys.stderr,
                )
            if args.force_id == "M5":
                args.force_id = "LC"
            force_expected = True
            try:
                force_ch = open_loadcell_channel(
                    args.force_port,
                    range_kg=loadcell_range_kg,
                    baud=args.force_baud,
                    slave_id=args.loadcell_addr,
                    decimals=args.loadcell_decimals,
                    debug=args.loadcell_debug,
                    raw_cal=raw_cal,
                )
                print(
                    f"  Force port: {args.force_port}  "
                    f"(RS485 load cell, {loadcell_range_kg:g} kg FS, addr {args.loadcell_addr})"
                )
            except OSError as exc:
                print(f"  Load cell not connected: {exc}", file=sys.stderr)
                print(
                    f"  Waiting for {args.force_port} — plug in the adapter anytime; "
                    "logging starts automatically."
                )
        else:
            force_port = args.force_port or auto_force_port()
            if force_port:
                try:
                    force_ch = open_force_channel(force_port, baud=args.force_baud)
                    print(f"  Force port: {force_port}")
                except OSError as exc:
                    print(f"  Warning: could not open Mark-10 on {force_port}: {exc}", file=sys.stderr)
            elif args.force_port:
                sys.exit(f"Could not open Mark-10 port {args.force_port!r}")
            else:
                print(
                    "  No Mark-10 found (displacement only). "
                    "Connect gauge USB or use --force-port / --force-type loadcell.",
                    file=sys.stderr,
                )

    if args.no_displacement and force_ch is None and not force_expected:
        sys.exit(
            "Force-only mode requires a force sensor.\n"
            "Example:  spc-plot --no-displacement --force-type loadcell "
            "--force-port /dev/ttyUSB0 --loadcell-range 100kg"
        )

    has_disp = ch is not None
    # In load cell mode the force channel counts as present even while the
    # adapter is unplugged — datasets/axes are created up front and samples
    # start flowing the moment the device (re)appears.
    has_force = force_ch is not None or force_expected

    data_file = resolve_data_path(args.data_file)
    logger = DataLogger(
        data_file,
        args.device_id,
        args.hz,
        args.tag,
        args.ch_name,
        log_displacement=has_disp,
        log_force=has_force,
        force_device_id=args.force_id,
        force_ch_name=args.force_ch_name,
        force_capacity_n=capacity_n(loadcell_range_kg) if loadcell_range_kg else None,
        log_counts=loadcell_range_kg is not None,
        force_cal=raw_cal,
    )

    max_points = int(args.window * args.hz * 1.1)
    times = collections.deque(maxlen=max_points)
    mm_vals = collections.deque(maxlen=max_points)
    n_vals = collections.deque(maxlen=max_points)

    is_loadcell = loadcell_range_kg is not None
    fmax = args.fmax
    if fmax is None:
        fmax = DEFAULT_LOADCELL_MAX_N if is_loadcell else DEFAULT_FORCE_MAX_N
    if args.fmin is not None:
        fmin = args.fmin
    else:
        # Load cell reads positive-only; Mark-10 is bipolar (tension/compression).
        fmin = 0.0 if is_loadcell else -fmax

    if has_disp and has_force:
        title = "SPC displacement"
        title += " + load cell force" if args.force_type == "loadcell" else " + Mark-10 force"
    elif has_disp:
        title = "SPC displacement"
    else:
        title = "Load cell force" if args.force_type == "loadcell" else "Force"

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

    line_disp = None
    line_force = None
    ax_disp = ax_main if has_disp else None
    ax_force = None
    dual = has_disp and has_force

    # In dual-channel mode each y-axis is tinted to its series so they can be
    # told apart; a lone axis wears plain text color.
    if has_disp:
        line_disp, = ax_main.plot([], [], color=DISP_COLOR, linewidth=2, label=args.ch_name)
        ax_main.set_ylim(args.ymin, args.ymax)
        disp_ink = DISP_COLOR if dual else INK
        ax_main.set_ylabel(UNIT_MM, fontsize=15, color=disp_ink)
        ax_main.tick_params(axis="y", labelcolor=disp_ink)

    if has_force:
        if has_disp:
            ax_force = ax_main.twinx()
        else:
            ax_force = ax_main
        line_force, = ax_force.plot(
            [], [], color=FORCE_COLOR, linewidth=2, label=args.force_ch_name,
        )
        ax_force.set_ylim(fmin, fmax)
        force_ink = FORCE_COLOR if dual else INK
        ax_force.set_ylabel(UNIT_N, fontsize=15, color=force_ink)
        ax_force.tick_params(axis="y", labelcolor=force_ink)

    for ax in {ax_main, ax_force} - {None}:
        ax.spines["top"].set_visible(False)
        for spine in ax.spines.values():
            spine.set_color(BORDER)
        ax.tick_params(color=BORDER, labelsize=13)
    ax_main.tick_params(axis="x", labelcolor=INK_MUTED)
    if not dual:
        ax_main.spines["right"].set_visible(False)

    legend_ax = ax_main
    legend_lines = [ln for ln in (line_disp, line_force) if ln is not None]
    legend_labels = []
    if line_disp is not None:
        legend_labels.append(args.ch_name)
    if line_force is not None:
        legend_labels.append(args.force_ch_name)
    if legend_lines:
        legend_ax.legend(legend_lines, legend_labels, loc="upper right",
                         fontsize=13, frameon=False, labelcolor=INK)

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
            f"Load cell not connected — waiting for {args.force_port}"
        )
        wait_text.set_visible(force_ch is None)

    hover_color = DISP_COLOR if has_disp else FORCE_COLOR
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
    dyn_artists = tuple(
        a for a in (line_disp, line_force, readout, status_text, wait_text,
                    hover_dot, hover_ann)
        if a is not None
    )
    for _a in dyn_artists:
        _a.set_animated(True)

    render = {"bg": None}

    def _draw_dynamic() -> None:
        for a in dyn_artists:
            if a.get_visible():
                a.axes.draw_artist(a)

    def on_draw(_event=None) -> None:
        # After any full draw (startup, resize, expose, widget typing) re-cache the
        # background and immediately repaint the dynamic artists on top of it.
        render["bg"] = fig.canvas.copy_from_bbox(fig.bbox)
        _draw_dynamic()
        fig.canvas.blit(fig.bbox)

    fig.canvas.mpl_connect("draw_event", on_draw)

    def blit_frame() -> None:
        if render["bg"] is None:
            fig.canvas.draw()   # triggers on_draw, which caches bg and blits
            return
        fig.canvas.restore_region(render["bg"])
        _draw_dynamic()
        fig.canvas.blit(fig.bbox)

    def full_refresh() -> None:
        # Force a full redraw (picks up title/axis/widget changes), then blit.
        fig.canvas.draw()

    # live["x"] = relative seconds (plot coords), live["t_unix"] = absolute time
    # for tooltips. Use len(), never truthiness.
    live = {"x": np.empty(0), "t_unix": np.empty(0), "mm": np.empty(0), "n": np.empty(0)}

    def on_hover(event):
        hover_axes = tuple(a for a in (ax_disp, ax_force) if a is not None)
        if event.inaxes not in hover_axes or len(live["x"]) == 0:
            hover_dot.set_data([], [])
            hover_ann.set_visible(False)
            blit_frame()
            return

        x_arr = live["x"]
        if has_disp and len(live["mm"]):
            y_arr = live["mm"]
            hover_ax = ax_disp
        elif has_force and len(live["n"]):
            y_arr = live["n"]
            hover_ax = ax_force
        else:
            hover_dot.set_data([], [])
            hover_ann.set_visible(False)
            blit_frame()
            return

        x_display = hover_ax.transData.transform(np.column_stack([x_arr, y_arr]))[:, 0]
        idx = int(np.argmin(np.abs(x_display - event.x)))

        tx = x_arr[idx]
        ts = datetime.datetime.fromtimestamp(
            live["t_unix"][idx]
        ).strftime("%H:%M:%S.%f")[:-3]
        lines_txt = [ts]
        if has_disp and len(live["mm"]):
            vy_mm = live["mm"][idx]
            mm_str = f"{vy_mm:.4f} {UNIT_MM}" if not np.isnan(vy_mm) else "No data"
            lines_txt.append(f"{args.ch_name}: {mm_str}")
        if has_force and len(live["n"]):
            vy_f = live["n"][idx] if idx < len(live["n"]) else float("nan")
            f_str = f"{vy_f:.4f} {UNIT_N}" if not np.isnan(vy_f) else "No data"
            lines_txt.append(f"{args.force_ch_name}: {f_str}")

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
        mm_vals.clear()
        n_vals.clear()
        live["x"] = np.empty(0)
        live["t_unix"] = np.empty(0)
        live["mm"] = np.empty(0)
        live["n"] = np.empty(0)
        if line_disp is not None:
            line_disp.set_data([], [])
        if line_force is not None:
            line_force.set_data([], [])
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

    # Serial acquisition runs on a background thread and hands samples
    # (t, mm, force_n, force_counts) to the GUI via this queue, so blocking
    # reads never freeze inputs/redraws.
    sample_q: "queue.Queue[tuple[float, float | None, float | None, float | None]]" = queue.Queue()
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
    print("  Press t to stop/start a run; s to save the label; q to quit.")

    GUI_INTERVAL_MS = 25  # ~40 fps; blitting keeps each frame cheap
    period = 1.0 / args.hz

    def connect_force() -> ForceChannel | None:
        """Try to (re)open the load cell; None while it is absent/unresponsive."""
        if not port_present(args.force_port):
            return None
        try:
            fch = open_loadcell_channel(
                args.force_port,
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

    def sampler_loop() -> None:
        """Read the (slow, blocking) serial channels off the GUI thread."""
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
                        print(f"  Load cell connected on {args.force_port}")
                if fch is None and ch is None:
                    # Force-only mode with no device: idle (no NaN rows in the
                    # log) until the adapter shows up.
                    stop_evt.wait(0.1)
                    next_t = time.monotonic()
                    continue

            mm = poll_channel(ch) if ch else None
            force_n = None
            counts = None
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
                        f"  Load cell unplugged — waiting for {args.force_port} "
                        "to reappear",
                        file=sys.stderr,
                    )
                    if ch is None:
                        next_t = time.monotonic()
                        continue  # force-only: nothing to record this tick
                else:
                    if read_exc is not None:
                        print(f"  Force read error on {fch.port}: {read_exc}",
                              file=sys.stderr)
                    force_n = to_n(reading)
                    counts = getattr(fch, "last_counts", None)
            sample_q.put((time.time(), mm, force_n, counts))
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                stop_evt.wait(delay)   # interruptible sleep; wakes on shutdown
            else:
                next_t = time.monotonic()  # fell behind (I/O slower than 1/hz)

    sampler = threading.Thread(target=sampler_loop, name="spc-sampler", daemon=True)

    def fmt_mm(v: float) -> str:
        if np.isnan(v):
            return "No data"
        return f"{v:.4f} {UNIT_MM}"

    def fmt_n(v: float) -> str:
        if np.isnan(v):
            return "No data"
        return f"{v:.4f} {UNIT_N}"

    def on_timer() -> None:
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
                t_unix, mm, force_n, counts = sample_q.get_nowait()
            except queue.Empty:
                break
            last_t_unix = t_unix
            times.append(t_unix)   # store absolute unix seconds
            if has_disp:
                mm_vals.append(mm if mm is not None else float("nan"))
            if has_force:
                n_vals.append(force_n if force_n is not None else float("nan"))
            logger.append(t_unix, mm, force_n, counts)

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
        if line_disp is not None:
            mm_arr = thin(mm_vals)
            line_disp.set_data(x_rel, mm_arr)
            live["mm"] = mm_arr
        if line_force is not None:
            n_arr = thin(n_vals)
            line_force.set_data(x_rel, n_arr)
            live["n"] = n_arr

        t_last = datetime.datetime.fromtimestamp(last_t_unix)
        readout_lines = []
        if has_disp and mm_vals:
            readout_lines.append(f"{args.ch_name}: {fmt_mm(mm_vals[-1])}")
        if has_force and n_vals:
            readout_lines.append(f"{args.force_ch_name}: {fmt_n(n_vals[-1])}")
        readout_lines.append(t_last.strftime("%H:%M:%S.%f")[:-3])
        readout.set_text("\n".join(readout_lines))
        blit_frame()

    timer = fig.canvas.new_timer(interval=GUI_INTERVAL_MS)
    timer.add_callback(on_timer)
    fig._spc_timer = timer  # keep a reference so the timer stays alive

    fig.tight_layout(rect=(0, 0, 1, 0.862))

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
    print("  Type a note in the 'Cycle label' field; press s to save it to the current run.")

    def on_close(_event) -> None:
        # Stop the timer and sampler the moment the window closes (q, cmd+w,
        # or the close button). An active timer can keep the macosx event loop
        # alive after the last window closes, leaving the process running with
        # the serial port open — killing it then wedges the FTDI driver until
        # the adapter is replugged (open() fails with termios EINVAL).
        timer.stop()
        stop_evt.set()

    fig.canvas.mpl_connect("close_event", on_close)

    # If the process dies by signal (closed terminal → SIGHUP, kill → SIGTERM),
    # exit via SystemExit so the finally block below still closes the port.
    for _sig in (signal.SIGTERM, signal.SIGHUP):
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
        for c in (ch, force["ch"]):
            if c is not None:
                try:
                    c.close()
                except OSError:
                    pass


if __name__ == "__main__":
    main()
