#!/usr/bin/env python3
"""Real-time Mitutoyo displacement + Mark-10 force plotter and HDF5 logger."""

import argparse
import collections
import datetime
import os
import sys
import time

from . import mpl_setup  # noqa: F401  # before matplotlib.pyplot

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.animation import FuncAnimation

try:
    import h5py
except ImportError:
    sys.exit("h5py not found. Run:  pip install h5py  (or: pip install -r requirements.txt)")

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
    FORCE_DATASET,
    Reading,
    SPCChannel,
    auto_port,
    format_device_list,
    open_channel,
)

DISP_COLOR = "tomato"
FORCE_COLOR = "steelblue"
UNIT_MM = "mm"
UNIT_N = "N"
# M5-10 full scale: 10 lbf ≈ 44.5 N
DEFAULT_FORCE_MAX_N = 10 * 4.4482216152605


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
        log_force: bool = False,
        force_device_id: str = "",
        force_ch_name: str = "Force",
    ):
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        self._path = filepath
        self._device_id = device_id
        self._hz = hz
        self._tag = tag
        self._ch_name = ch_name
        self._log_force = log_force
        self._force_device_id = force_device_id
        self._force_ch_name = force_ch_name
        self._buf_t: list[float] = []
        self._buf_mm: list[float] = []
        self._buf_n: list[float] = []
        self._start_cycle()

    def _start_cycle(self) -> None:
        with h5py.File(self._path, "a") as f:
            cycles = f.require_group("cycles")
            cycle_idx = len(cycles) + 1
            grp_name = f"{cycle_idx:06d}"
            grp = cycles.create_group(grp_name)
            grp.attrs["tag"]          = self._tag or ""
            grp.attrs["serial"]       = self._device_id
            grp.attrs["hz"]           = self._hz
            grp.attrs["start_iso"]    = datetime.datetime.now().isoformat()
            grp.attrs["units"]        = UNIT_MM
            grp.attrs["channel_name"] = self._ch_name
            if self._log_force:
                grp.attrs["force_serial"]       = self._force_device_id
                grp.attrs["force_units"]        = UNIT_N
                grp.attrs["force_channel_name"] = self._force_ch_name
            datasets: list[tuple[str, str]] = [
                ("time", "f8"),
                (DISPLACEMENT_DATASET, "f4"),
            ]
            if self._log_force:
                datasets.append((FORCE_DATASET, "f4"))
            for ds_name, dtype in datasets:
                grp.create_dataset(
                    ds_name, shape=(0,), maxshape=(None,),
                    dtype=dtype, chunks=(256,), compression="gzip",
                )
            self._grp_path = f"cycles/{grp_name}"

        tag_display = f"  tag={self._tag!r}" if self._tag else ""
        print(f"Logging → {self._path}  [{self._grp_path}]{tag_display}")

    def new_cycle(self) -> str:
        """Flush the current cycle and begin a new HDF5 group."""
        self._flush()
        self._start_cycle()
        return self._grp_path

    def append(
        self,
        unix_t: float,
        displacement_mm: float,
        force_n: float | None = None,
    ) -> None:
        self._buf_t.append(unix_t)
        self._buf_mm.append(displacement_mm)
        if self._log_force:
            self._buf_n.append(force_n if force_n is not None else float("nan"))
        if len(self._buf_t) >= self.FLUSH_EVERY:
            self._flush()

    def close(self) -> None:
        self._flush()

    def _flush(self) -> None:
        if not self._buf_t:
            return
        t_data = self._buf_t[:]
        mm_data = self._buf_mm[:]
        self._buf_t.clear()
        self._buf_mm.clear()
        pairs: list[tuple[str, list]] = [
            ("time", t_data),
            (DISPLACEMENT_DATASET, mm_data),
        ]
        if self._log_force:
            n_data = self._buf_n[:]
            self._buf_n.clear()
            pairs.append((FORCE_DATASET, n_data))
        with h5py.File(self._path, "a") as f:
            grp = f[self._grp_path]
            for ds_name, data in pairs:
                ds = grp[ds_name]
                n = len(ds)
                ds.resize(n + len(data), axis=0)
                ds[n:] = data


def parse_args():
    p = argparse.ArgumentParser(
        description="Real-time displacement (Mitutoyo SPC) and force (Mark-10) plotter",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--port", metavar="PATH",
                   help="usb-itn, usb-itn:SERIAL, or /dev/tty* (default: auto)")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--list-ports", action="store_true",
                   help="List Mitutoyo and Mark-10 devices and exit")
    p.add_argument("--device-id", default="SPC", metavar="ID")
    p.add_argument("--window", type=float, default=600, metavar="SECS")
    p.add_argument("--ymin", type=float, default=0, metavar="MM")
    p.add_argument("--ymax", type=float, default=30, metavar="MM")
    p.add_argument("--hz", type=float, default=20, metavar="HZ")
    p.add_argument("--ch-name", default="SPC", metavar="NAME", help="Displacement legend")
    p.add_argument("--tag", default="", help="Free-text label for this cycle")
    p.add_argument("--force-port", metavar="PATH",
                   help="Mark-10 USB serial port (default: auto if one gauge found)")
    p.add_argument("--no-force", action="store_true",
                   help="Displacement only (skip Mark-10)")
    p.add_argument("--force-baud", type=int, default=115200)
    p.add_argument("--force-id", default="M5", metavar="ID")
    p.add_argument("--force-ch-name", default="Force", metavar="NAME")
    p.add_argument("--fmin", type=float, default=None, metavar="N",
                   help="Force Y-axis min (default: ±M5-10 full scale in N)")
    p.add_argument("--fmax", type=float, default=None, metavar="N")
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


def poll_force(ch: ForceChannel) -> float:
    try:
        return to_n(ch.read())
    except OSError as exc:
        print(f"  Force read error on {ch.port}: {exc}", file=sys.stderr)
        return float("nan")


def main():
    args = parse_args()

    if args.list_ports:
        print(format_device_list())
        print()
        print(format_force_device_list())
        return

    port = args.port or auto_port()
    if not port:
        print(format_device_list(), file=sys.stderr)
        sys.exit(
            "No Mitutoyo device found.\n"
            "Install udev rules (see README), replug USB-ITN, and run:  --list-ports"
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

    force_ch: ForceChannel | None = None
    if not args.no_force:
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
                "Connect gauge USB or use --force-port.",
                file=sys.stderr,
            )

    data_file = resolve_data_path(args.data_file)
    logger = DataLogger(
        data_file,
        args.device_id,
        args.hz,
        args.tag,
        args.ch_name,
        log_force=force_ch is not None,
        force_device_id=args.force_id,
        force_ch_name=args.force_ch_name,
    )

    max_points = int(args.window * args.hz * 1.1)
    times = collections.deque(maxlen=max_points)
    mm_vals = collections.deque(maxlen=max_points)
    n_vals = collections.deque(maxlen=max_points)

    fmax = args.fmax if args.fmax is not None else DEFAULT_FORCE_MAX_N
    fmin = args.fmin if args.fmin is not None else -fmax

    title = "SPC displacement"
    if force_ch is not None:
        title += " + Mark-10 force"
    fig, ax_disp = plt.subplots(num=title, figsize=(13, 6))
    fig.patch.set_facecolor("#F0F0F0")
    ax_disp.set_facecolor("#F0F0F0")

    line_disp, = ax_disp.plot([], [], color=DISP_COLOR, linewidth=2, label=args.ch_name)
    ax_disp.set_ylim(args.ymin, args.ymax)
    ax_disp.set_ylabel(UNIT_MM, fontsize=17, fontweight="bold", color=DISP_COLOR)
    ax_disp.tick_params(axis="y", labelcolor=DISP_COLOR)

    ax_force = None
    line_force = None
    if force_ch is not None:
        ax_force = ax_disp.twinx()
        line_force, = ax_force.plot(
            [], [], color=FORCE_COLOR, linewidth=2, label=args.force_ch_name,
        )
        ax_force.set_ylim(fmin, fmax)
        ax_force.set_ylabel(UNIT_N, fontsize=17, fontweight="bold", color=FORCE_COLOR)
        ax_force.tick_params(axis="y", labelcolor=FORCE_COLOR)
        lines = [line_disp, line_force]
        labels = [args.ch_name, args.force_ch_name]
        ax_disp.legend(lines, labels, loc="upper right", fontsize=14)
    else:
        ax_disp.legend(loc="upper right", fontsize=14)

    ax_disp.set_xlabel(f"Time  ({int(args.window)} s rolling window)", fontsize=17, fontweight="bold")
    ax_disp.set_title(args.tag if args.tag else "", fontsize=16)
    ax_disp.yaxis.grid(True, color="#D2D2D2", linewidth=1)
    ax_disp.xaxis.grid(False)
    ax_disp.tick_params(labelsize=14)
    ax_disp.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    fig.autofmt_xdate()

    readout = ax_disp.text(
        0.01, 0.97, "", transform=ax_disp.transAxes,
        fontsize=14, verticalalignment="top", family="monospace",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.82, edgecolor="black"),
    )

    hover_dot_disp, = ax_disp.plot([], [], "o", color=DISP_COLOR, markersize=7, zorder=5)
    hover_ann = ax_disp.annotate(
        "", xy=(0, 0), xytext=(8, 8), textcoords="offset points",
        fontsize=14,
        bbox=dict(boxstyle="round,pad=0.4", facecolor="white", edgecolor="gray", alpha=0.9),
        visible=False,
    )

    live = {"t": [], "mm": [], "n": []}

    def on_hover(event):
        if event.inaxes not in (ax_disp, ax_force) or not live["t"]:
            hover_dot_disp.set_data([], [])
            hover_ann.set_visible(False)
            fig.canvas.draw_idle()
            return

        t_arr = np.array(live["t"])
        mm_arr = np.array(live["mm"])
        x_display = ax_disp.transData.transform(np.column_stack([t_arr, mm_arr]))[:, 0]
        idx = int(np.argmin(np.abs(x_display - event.x)))

        tx = t_arr[idx]
        vy_mm = mm_arr[idx]
        ts = mdates.num2date(tx).strftime("%H:%M:%S.%f")[:-3]
        mm_str = f"{vy_mm:.4f} {UNIT_MM}" if not np.isnan(vy_mm) else "No data"
        lines_txt = [ts, f"{args.ch_name}: {mm_str}"]
        if force_ch is not None and live["n"]:
            vy_f = live["n"][idx] if idx < len(live["n"]) else float("nan")
            f_str = f"{vy_f:.4f} {UNIT_N}" if not np.isnan(vy_f) else "No data"
            lines_txt.append(f"{args.force_ch_name}: {f_str}")

        if not np.isnan(vy_mm):
            hover_dot_disp.set_data([tx], [vy_mm])
        else:
            hover_dot_disp.set_data([], [])
        hover_ann.xy = (tx, vy_mm)
        hover_ann.set_text("\n".join(lines_txt))
        hover_ann.set_visible(True)

        ax_ext = ax_disp.get_window_extent()
        x_frac = (x_display[idx] - ax_ext.x0) / ax_ext.width
        x_off = -8 if x_frac > 0.5 else 8
        hover_ann.xyann = (x_off, 8)
        hover_ann.set_ha("right" if x_off < 0 else "left")
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("motion_notify_event", on_hover)

    def reset_display() -> None:
        times.clear()
        mm_vals.clear()
        n_vals.clear()
        live["t"] = []
        live["mm"] = []
        live["n"] = []
        line_disp.set_data([], [])
        if line_force is not None:
            line_force.set_data([], [])
        hover_dot_disp.set_data([], [])
        hover_ann.set_visible(False)
        readout.set_text("")
        fig.canvas.draw_idle()

    def on_key(event):
        if event.key != "r":
            return
        reset_display()
        grp = logger.new_cycle()
        print(f"  New cycle → [{grp}]  (plot cleared; press r again anytime)")

    fig.canvas.mpl_connect("key_press_event", on_key)
    print("  Press r to flush the log and start a new cycle (clears the plot).")

    GUI_INTERVAL_MS = 50
    period = 1.0 / args.hz
    next_sample = [0.0]

    def fmt_mm(v: float) -> str:
        if np.isnan(v):
            return "No data"
        return f"{v:.4f} {UNIT_MM}"

    def fmt_n(v: float) -> str:
        if np.isnan(v):
            return "No data"
        return f"{v:.4f} {UNIT_N}"

    def update(_frame):
        now = time.monotonic()
        if now < next_sample[0]:
            return (line_disp, readout)
        next_sample[0] = now + period

        mm = poll_channel(ch)
        force_n = poll_force(force_ch) if force_ch else None
        t_unix = time.time()
        t_mpl = mdates.date2num(datetime.datetime.fromtimestamp(t_unix))
        times.append(t_mpl)
        mm_vals.append(mm)
        if force_ch is not None:
            n_vals.append(force_n if force_n is not None else float("nan"))
        logger.append(t_unix, mm, force_n)

        if len(times) < 2:
            return (line_disp, readout)

        cutoff = mdates.date2num(
            datetime.datetime.now() - datetime.timedelta(seconds=args.window)
        )
        vis_t = [t for t in times if t >= cutoff]
        vis_mm = [v for t, v in zip(times, mm_vals) if t >= cutoff]
        vis_n = [v for t, v in zip(times, n_vals) if t >= cutoff] if force_ch else []

        line_disp.set_data(vis_t, vis_mm)
        if line_force is not None:
            line_force.set_data(vis_t, vis_n)
        live["t"] = vis_t
        live["mm"] = vis_mm
        live["n"] = vis_n

        ax_disp.set_xlim(
            mdates.date2num(
                datetime.datetime.now() - datetime.timedelta(seconds=args.window)
            ),
            mdates.date2num(datetime.datetime.now()),
        )

        t_last = datetime.datetime.fromtimestamp(t_unix)
        readout_lines = [f"{args.ch_name}: {fmt_mm(mm_vals[-1])}"]
        if force_ch is not None:
            readout_lines.append(f"{args.force_ch_name}: {fmt_n(n_vals[-1])}")
        readout_lines.append(t_last.strftime("%H:%M:%S.%f")[:-3])
        readout.set_text("\n".join(readout_lines))
        return (line_disp, readout)

    anim = FuncAnimation(
        fig, update, interval=GUI_INTERVAL_MS, blit=False, cache_frame_data=False,
    )
    fig._spc_anim = anim

    plt.tight_layout()
    try:
        plt.show()
    finally:
        logger.close()
        ch.close()
        if force_ch is not None:
            force_ch.close()


if __name__ == "__main__":
    main()
