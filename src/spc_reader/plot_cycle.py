#!/usr/bin/env python3
"""Plot a recorded displacement (and optional force) cycle from the HDF5 log.

Usage
-----
    python3 scripts/plot_cycle.py              # list cycles
    python3 scripts/plot_cycle.py 3           # plot cycle 3
    python3 scripts/plot_cycle.py last        # most recent
    python3 scripts/plot_cycle.py --data-file /path/to/log.h5 3
"""

import argparse
import csv
import os
import sys

from . import mpl_setup  # noqa: F401  # before matplotlib.pyplot

import h5py
import numpy as np
import matplotlib.pyplot as plt

from .paths import default_log_path, resolve_data_path
from .spc_io import LBF_TO_N, read_displacement_mm, read_force_n, try_read_displacement_mm

DISP_COLOR = "#eb6834"   # orange — displacement series (matches plot_live)
FORCE_COLOR = "#2a78d6"  # blue — force series (matches plot_live)


def load_cycles(path: str) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"Data file not found: {path}")
    cycles = []
    with h5py.File(path, "r") as f:
        grp = f.get("cycles")
        if grp is None or len(grp) == 0:
            sys.exit("No cycles found in the data file.")
        for name in sorted(grp.keys()):
            g = grp[name]
            t = g["time"][:]
            mm = try_read_displacement_mm(g)
            force_n = read_force_n(g)
            has_disp = mm is not None and len(mm) > 0
            duration = t[-1] - t[0] if len(t) > 1 else 0
            entry = {
                "name":      name,
                "tag":       g.attrs.get("tag", ""),
                "serial":    g.attrs.get("serial", ""),
                "hz":        float(g.attrs.get("hz", 0)),
                "start_iso": g.attrs.get("start_iso", ""),
                "n":         len(t),
                "duration":  duration,
                "has_disp":  has_disp,
                "has_force": force_n is not None and len(force_n) > 0,
            }
            if has_disp:
                entry["mm_min"] = float(mm.min())
                entry["mm_max"] = float(mm.max())
                entry["mm_mean"] = float(mm.mean())
            if entry["has_force"]:
                entry["n_min"] = float(force_n.min())
                entry["n_max"] = float(force_n.max())
                entry["n_mean"] = float(force_n.mean())
            cycles.append(entry)
    return cycles


def list_cycles(cycles: list[dict]) -> None:
    has_force = any(c["has_force"] for c in cycles)
    has_disp = any(c.get("has_disp", True) for c in cycles)
    if has_disp and has_force:
        header = (
            f"{'#':>6}  {'Start':>19}  {'Dur (s)':>8}  "
            f"{'Min mm':>7}  {'Max mm':>7}  {'Min N':>8}  {'Max N':>8}  "
            f"{'n':>6}  Tag"
        )
    elif has_force:
        header = (
            f"{'#':>6}  {'Start':>19}  {'Dur (s)':>8}  "
            f"{'Min N':>8}  {'Max N':>8}  {'Mean N':>8}  {'n':>6}  Tag"
        )
    else:
        header = (
            f"{'#':>6}  {'Start':>19}  {'Dur (s)':>8}  "
            f"{'Min mm':>7}  {'Max mm':>7}  {'Mean mm':>8}  {'n':>6}  Tag"
        )
    print(header)
    print("-" * len(header))
    for c in cycles:
        idx = int(c["name"])
        start = c["start_iso"][:19].replace("T", " ") if c["start_iso"] else "?"
        tag = c["tag"] or "—"
        if has_disp and has_force and c.get("has_disp") and c["has_force"]:
            print(
                f"{idx:>6}  {start:>19}  {c['duration']:>8.1f}"
                f"  {c['mm_min']:>7.2f}  {c['mm_max']:>7.2f}"
                f"  {c['n_min']:>8.3f}  {c['n_max']:>8.3f}"
                f"  {c['n']:>6}  {tag}"
            )
        elif has_disp and has_force:
            mm_min = c.get("mm_min", float("nan"))
            mm_max = c.get("mm_max", float("nan"))
            n_min = c.get("n_min", float("nan"))
            n_max = c.get("n_max", float("nan"))
            print(
                f"{idx:>6}  {start:>19}  {c['duration']:>8.1f}"
                f"  {mm_min:>7.2f}  {mm_max:>7.2f}"
                f"  {n_min:>8.3f}  {n_max:>8.3f}"
                f"  {c['n']:>6}  {tag}"
            )
        elif has_force and c["has_force"]:
            print(
                f"{idx:>6}  {start:>19}  {c['duration']:>8.1f}"
                f"  {c['n_min']:>8.3f}  {c['n_max']:>8.3f}  {c['n_mean']:>8.3f}"
                f"  {c['n']:>6}  {tag}"
            )
        elif c.get("has_disp"):
            print(
                f"{idx:>6}  {start:>19}  {c['duration']:>8.1f}"
                f"  {c['mm_min']:>7.2f}  {c['mm_max']:>7.2f}  {c['mm_mean']:>8.2f}"
                f"  {c['n']:>6}  {tag}"
            )


def load_data(path: str, name: str):
    with h5py.File(path, "r") as f:
        grp = f[f"cycles/{name}"]
        t = grp["time"][:]
        mm = try_read_displacement_mm(grp)
        force_n = read_force_n(grp)
        meta = {k: grp.attrs[k] for k in grp.attrs}
    return t, mm, force_n, meta


def export_cycle_csv(
    t: np.ndarray,
    mm: np.ndarray | None,
    force_n: np.ndarray | None,
    meta: dict,
    name: str,
    out: str | None = None,
) -> str:
    """Write one cycle's time-series data to a CSV file.

    Parameters
    ----------
    t:       Unix epoch timestamps (float64).
    mm:      Displacement values in mm.
    force_n: Force values in N, or None if not recorded.
    meta:    HDF5 group attributes dict.
    name:    Zero-padded HDF5 group name, e.g. ``"000003"``.
    out:     Output path.  Defaults to ``cycle_<N>.csv`` in the current directory.

    Returns
    -------
    str
        Absolute path of the written file.
    """
    cycle_num = int(name)
    if out is None:
        out = f"cycle_{cycle_num}.csv"
    out = os.path.abspath(out)

    has_disp = mm is not None and len(mm) > 0
    has_force = force_n is not None and len(force_n) > 0

    with open(out, "w", newline="") as fh:
        # Metadata comment header
        fh.write(f"# cycle: {cycle_num}\n")
        fh.write(f"# tag: {meta.get('tag', '')}\n")
        fh.write(f"# start: {meta.get('start_iso', '')}\n")
        if has_disp:
            fh.write(f"# serial: {meta.get('serial', '')}\n")
            fh.write(f"# displacement_units: {meta.get('units', 'mm')}\n")
        fh.write(f"# hz: {meta.get('hz', '')}\n")
        if has_force:
            fh.write(f"# force_serial: {meta.get('force_serial', '')}\n")
            fh.write(f"# force_units: {meta.get('force_units', 'N')}\n")

        fieldnames = ["time_unix", "elapsed_s"]
        if has_disp:
            fieldnames.append("displacement_mm")
        if has_force:
            fieldnames.append("force_n")

        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()

        t0 = float(t[0]) if len(t) else 0.0
        for i in range(len(t)):
            row: dict = {
                "time_unix": f"{t[i]:.6f}",
                "elapsed_s": f"{t[i] - t0:.6f}",
            }
            if has_disp:
                row["displacement_mm"] = f"{mm[i]:.6f}"
            if has_force:
                row["force_n"] = f"{force_n[i]:.6f}"
            writer.writerow(row)

    return out


def fmt_elapsed(seconds: float) -> str:
    s = abs(seconds)
    if s < 3600:
        m, rem = divmod(s, 60)
        return f"{int(m)}:{rem:04.1f}"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{sec:04.1f}"


def plot_cycle(
    t_unix: np.ndarray,
    mm: np.ndarray | None,
    force_n: np.ndarray | None,
    meta: dict,
    name: str,
) -> None:
    t_rel = t_unix - t_unix[0]
    duration_s = t_rel[-1] if len(t_rel) > 1 else 0

    tag = meta.get("tag", "")
    start = meta.get("start_iso", "")[:19].replace("T", " ")
    disp_label = meta.get("channel_name", meta.get("ch1_name", "SPC"))
    force_label = meta.get("force_channel_name", "Force")
    has_disp = mm is not None and len(mm) > 0
    has_force = force_n is not None and len(force_n) > 0

    if has_disp and has_force:
        title = "Displacement + force cycle"
    elif has_force:
        title = "Force cycle"
    else:
        title = "SPC displacement cycle"
    fig, ax_main = plt.subplots(num=title, figsize=(13, 6))
    fig.patch.set_facecolor("#F0F0F0")
    ax_main.set_facecolor("#F0F0F0")

    ax_force = None
    if has_disp:
        ax_main.plot(t_rel, mm, color=DISP_COLOR, linewidth=1.5, label=disp_label)
        ax_main.set_ylim(0, 30)
        ax_main.set_ylabel("mm", fontsize=17, fontweight="bold", color=DISP_COLOR)
        ax_main.tick_params(axis="y", labelcolor=DISP_COLOR)

    if has_force:
        if has_disp:
            ax_force = ax_main.twinx()
        else:
            ax_force = ax_main
        ax_force.plot(t_rel, force_n, color=FORCE_COLOR, linewidth=1.5, label=force_label)
        cap = float(meta.get("force_capacity_n", 10 * LBF_TO_N))
        ax_force.set_ylim(-cap, cap)
        ax_force.set_ylabel("N", fontsize=17, fontweight="bold", color=FORCE_COLOR)
        ax_force.tick_params(axis="y", labelcolor=FORCE_COLOR)

    legend_lines = list(ax_main.lines)
    if ax_force is not None and ax_force is not ax_main:
        legend_lines.extend(ax_force.lines)
    legend_labels = []
    if has_disp:
        legend_labels.append(disp_label)
    if has_force:
        legend_labels.append(force_label)
    if legend_lines:
        ax_main.legend(legend_lines, legend_labels, loc="upper right", fontsize=14)

    nice = [1, 2, 5, 10, 15, 20, 30, 60, 120, 180, 300, 600, 900, 1800, 3600]
    tick_interval = min(nice, key=lambda x: abs(x - max(duration_s, 1) / 8))
    ax_main.xaxis.set_major_locator(plt.MultipleLocator(tick_interval))
    if tick_interval < 60:
        ax_main.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f} s"))
    else:
        ax_main.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: fmt_elapsed(x)))

    unit = "s" if tick_interval < 60 else "m:ss"
    ax_main.set_xlabel(f"Elapsed time ({unit})", fontsize=17, fontweight="bold")
    ax_main.yaxis.grid(True, color="#D2D2D2", linewidth=1)
    ax_main.xaxis.grid(False)
    ax_main.tick_params(labelsize=14)

    title_lines = [f"Cycle {int(name)} — started {start}   (duration {fmt_elapsed(duration_s)})"]
    if tag:
        title_lines.append(tag)
    ax_main.set_title("\n".join(title_lines), fontsize=16)

    stats_lines = []
    if has_disp:
        valid_mm = mm[~np.isnan(mm.astype(float))]
        if len(valid_mm):
            stats_lines.append(
                f"{disp_label}:  min {valid_mm.min():.2f}  max {valid_mm.max():.2f}"
                f"  mean {valid_mm.mean():.2f} mm  n={len(valid_mm):,}"
            )
        else:
            stats_lines.append(f"{disp_label}: no data")
    if has_force:
        valid_f = force_n[~np.isnan(force_n.astype(float))]
        if len(valid_f):
            stats_lines.append(
                f"{force_label}:  min {valid_f.min():.3f}  max {valid_f.max():.3f}"
                f"  mean {valid_f.mean():.3f} N  n={len(valid_f):,}"
            )
        else:
            stats_lines.append(f"{force_label}: no data")
    ax_main.text(
        0.01, 0.97, "\n".join(stats_lines), transform=ax_main.transAxes,
        fontsize=13, verticalalignment="top", family="monospace",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.82, edgecolor="black"),
    )

    hover_color = DISP_COLOR if has_disp else FORCE_COLOR
    hover_dot, = ax_main.plot([], [], "o", color=hover_color, markersize=7, zorder=5)
    hover_ann = ax_main.annotate(
        "", xy=(0, 0), xytext=(8, 8), textcoords="offset points",
        fontsize=14,
        bbox=dict(boxstyle="round,pad=0.4", facecolor="white", edgecolor="gray", alpha=0.9),
        visible=False,
    )

    def on_hover(event):
        hover_axes = tuple(a for a in (ax_main if has_disp else None, ax_force) if a is not None)
        if event.inaxes not in hover_axes:
            hover_dot.set_data([], [])
            hover_ann.set_visible(False)
            fig.canvas.draw_idle()
            return

        if has_disp:
            hover_ax = ax_main
            y_arr = mm
        else:
            hover_ax = ax_force
            y_arr = force_n

        x_display = hover_ax.transData.transform(np.column_stack([t_rel, y_arr]))[:, 0]
        idx = int(np.argmin(np.abs(x_display - event.x)))
        tx = t_rel[idx]
        lines_txt = [f"t = {fmt_elapsed(tx)}"]
        if has_disp:
            vy_mm = float(mm[idx])
            lines_txt.append(f"{disp_label}: {vy_mm:.2f} mm")
            vy = vy_mm
        if has_force:
            vy_f = float(force_n[idx])
            lines_txt.append(f"{force_label}: {vy_f:.3f} N")
            if not has_disp:
                vy = vy_f
        hover_dot.set_data([tx], [vy])
        hover_ann.xy = (tx, vy)
        hover_ann.set_text("\n".join(lines_txt))
        hover_ann.set_visible(True)

        ax_ext = hover_ax.get_window_extent()
        x_frac = (x_display[idx] - ax_ext.x0) / ax_ext.width
        x_off = -8 if x_frac > 0.5 else 8
        hover_ann.xyann = (x_off, 8)
        hover_ann.set_ha("right" if x_off < 0 else "left")
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("motion_notify_event", on_hover)
    plt.tight_layout()
    plt.show()


def parse_args():
    p = argparse.ArgumentParser(
        description="Plot a recorded displacement/force cycle from the HDF5 log.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "cycle", nargs="?", default=None,
        help="Cycle number to plot, or 'last'. Omit to list all cycles.",
    )
    p.add_argument(
        "--data-file", default=default_log_path(), metavar="PATH",
        help=f"HDF5 log file (default: {default_log_path()})",
    )
    p.add_argument(
        "--csv", nargs="?", const="", default=None, metavar="PATH",
        help=(
            "Export cycle to CSV instead of (or in addition to) plotting. "
            "PATH defaults to cycle_<N>.csv in the current directory."
        ),
    )
    return p.parse_args()


def main():
    args = parse_args()
    cycles = load_cycles(resolve_data_path(args.data_file))

    if args.cycle is None:
        list_cycles(cycles)
        return

    if args.cycle.lower() == "last":
        chosen = cycles[-1]
    else:
        try:
            idx = int(args.cycle)
        except ValueError:
            sys.exit(f"Invalid cycle: {args.cycle!r}  (use a number or 'last')")
        matches = [c for c in cycles if int(c["name"]) == idx]
        if not matches:
            sys.exit(f"Cycle {idx} not found. Available: {[int(c['name']) for c in cycles]}")
        chosen = matches[0]

    data_file = resolve_data_path(args.data_file)
    t, mm, force_n, meta = load_data(data_file, chosen["name"])

    if args.csv is not None:
        out_path = args.csv if args.csv else None
        written = export_cycle_csv(t, mm, force_n, meta, chosen["name"], out=out_path)
        print(f"Exported cycle {int(chosen['name'])} → {written}")
    else:
        plot_cycle(t, mm, force_n, meta, chosen["name"])


if __name__ == "__main__":
    main()
