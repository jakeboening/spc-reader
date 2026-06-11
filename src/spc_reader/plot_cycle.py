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
import os
import sys

from . import mpl_setup  # noqa: F401  # before matplotlib.pyplot

import h5py
import numpy as np
import matplotlib.pyplot as plt

from .paths import default_log_path, resolve_data_path
from .spc_io import LBF_TO_N, read_displacement_mm, read_force_n

DISP_COLOR = "tomato"
FORCE_COLOR = "steelblue"


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
            mm = read_displacement_mm(g)
            force_n = read_force_n(g)
            duration = t[-1] - t[0] if len(t) > 1 else 0
            entry = {
                "name":      name,
                "tag":       g.attrs.get("tag", ""),
                "serial":    g.attrs.get("serial", ""),
                "hz":        float(g.attrs.get("hz", 0)),
                "start_iso": g.attrs.get("start_iso", ""),
                "n":         len(t),
                "duration":  duration,
                "mm_min":    float(mm.min()) if len(mm) else float("nan"),
                "mm_max":    float(mm.max()) if len(mm) else float("nan"),
                "mm_mean":   float(mm.mean()) if len(mm) else float("nan"),
                "has_force": force_n is not None and len(force_n) > 0,
            }
            if entry["has_force"]:
                entry["n_min"] = float(force_n.min())
                entry["n_max"] = float(force_n.max())
                entry["n_mean"] = float(force_n.mean())
            cycles.append(entry)
    return cycles


def list_cycles(cycles: list[dict]) -> None:
    has_force = any(c["has_force"] for c in cycles)
    if has_force:
        header = (
            f"{'#':>6}  {'Start':>19}  {'Dur (s)':>8}  "
            f"{'Min mm':>7}  {'Max mm':>7}  {'Min N':>8}  {'Max N':>8}  "
            f"{'n':>6}  Tag"
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
        if has_force and c["has_force"]:
            print(
                f"{idx:>6}  {start:>19}  {c['duration']:>8.1f}"
                f"  {c['mm_min']:>7.2f}  {c['mm_max']:>7.2f}"
                f"  {c['n_min']:>8.3f}  {c['n_max']:>8.3f}"
                f"  {c['n']:>6}  {tag}"
            )
        elif has_force:
            print(
                f"{idx:>6}  {start:>19}  {c['duration']:>8.1f}"
                f"  {c['mm_min']:>7.2f}  {c['mm_max']:>7.2f}"
                f"  {'—':>8}  {'—':>8}"
                f"  {c['n']:>6}  {tag}"
            )
        else:
            print(
                f"{idx:>6}  {start:>19}  {c['duration']:>8.1f}"
                f"  {c['mm_min']:>7.2f}  {c['mm_max']:>7.2f}  {c['mm_mean']:>8.2f}"
                f"  {c['n']:>6}  {tag}"
            )


def load_data(path: str, name: str):
    with h5py.File(path, "r") as f:
        grp = f[f"cycles/{name}"]
        t = grp["time"][:]
        mm = read_displacement_mm(grp)
        force_n = read_force_n(grp)
        meta = {k: grp.attrs[k] for k in grp.attrs}
    return t, mm, force_n, meta


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
    mm: np.ndarray,
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

    title = "SPC displacement cycle"
    if force_n is not None:
        title = "Displacement + force cycle"
    fig, ax_disp = plt.subplots(num=title, figsize=(13, 6))
    fig.patch.set_facecolor("#F0F0F0")
    ax_disp.set_facecolor("#F0F0F0")

    ax_disp.plot(t_rel, mm, color=DISP_COLOR, linewidth=1.5, label=disp_label)
    ax_disp.set_ylim(0, 30)
    ax_disp.set_ylabel("mm", fontsize=17, fontweight="bold", color=DISP_COLOR)
    ax_disp.tick_params(axis="y", labelcolor=DISP_COLOR)

    ax_force = None
    if force_n is not None:
        ax_force = ax_disp.twinx()
        ax_force.plot(t_rel, force_n, color=FORCE_COLOR, linewidth=1.5, label=force_label)
        cap = float(meta.get("force_capacity_n", 10 * LBF_TO_N))
        ax_force.set_ylim(-cap, cap)
        ax_force.set_ylabel("N", fontsize=17, fontweight="bold", color=FORCE_COLOR)
        ax_force.tick_params(axis="y", labelcolor=FORCE_COLOR)
        ax_disp.legend(
            [ax_disp.lines[0], ax_force.lines[0]],
            [disp_label, force_label],
            loc="upper right",
            fontsize=14,
        )
    else:
        ax_disp.legend(loc="upper right", fontsize=14)

    nice = [1, 2, 5, 10, 15, 20, 30, 60, 120, 180, 300, 600, 900, 1800, 3600]
    tick_interval = min(nice, key=lambda x: abs(x - max(duration_s, 1) / 8))
    ax_disp.xaxis.set_major_locator(plt.MultipleLocator(tick_interval))
    if tick_interval < 60:
        ax_disp.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f} s"))
    else:
        ax_disp.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: fmt_elapsed(x)))

    unit = "s" if tick_interval < 60 else "m:ss"
    ax_disp.set_xlabel(f"Elapsed time ({unit})", fontsize=17, fontweight="bold")
    ax_disp.yaxis.grid(True, color="#D2D2D2", linewidth=1)
    ax_disp.xaxis.grid(False)
    ax_disp.tick_params(labelsize=14)

    title_lines = [f"Cycle {int(name)} — started {start}   (duration {fmt_elapsed(duration_s)})"]
    if tag:
        title_lines.append(tag)
    ax_disp.set_title("\n".join(title_lines), fontsize=16)

    valid_mm = mm[~np.isnan(mm.astype(float))]
    stats_lines = []
    if len(valid_mm):
        stats_lines.append(
            f"{disp_label}:  min {valid_mm.min():.2f}  max {valid_mm.max():.2f}"
            f"  mean {valid_mm.mean():.2f} mm  n={len(valid_mm):,}"
        )
    else:
        stats_lines.append(f"{disp_label}: no data")
    if force_n is not None:
        valid_f = force_n[~np.isnan(force_n.astype(float))]
        if len(valid_f):
            stats_lines.append(
                f"{force_label}:  min {valid_f.min():.3f}  max {valid_f.max():.3f}"
                f"  mean {valid_f.mean():.3f} N  n={len(valid_f):,}"
            )
        else:
            stats_lines.append(f"{force_label}: no data")
    ax_disp.text(
        0.01, 0.97, "\n".join(stats_lines), transform=ax_disp.transAxes,
        fontsize=13, verticalalignment="top", family="monospace",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.82, edgecolor="black"),
    )

    hover_dot, = ax_disp.plot([], [], "o", color=DISP_COLOR, markersize=7, zorder=5)
    hover_ann = ax_disp.annotate(
        "", xy=(0, 0), xytext=(8, 8), textcoords="offset points",
        fontsize=14,
        bbox=dict(boxstyle="round,pad=0.4", facecolor="white", edgecolor="gray", alpha=0.9),
        visible=False,
    )

    def on_hover(event):
        if event.inaxes not in (ax_disp, ax_force):
            hover_dot.set_data([], [])
            hover_ann.set_visible(False)
            fig.canvas.draw_idle()
            return

        x_display = ax_disp.transData.transform(np.column_stack([t_rel, mm]))[:, 0]
        idx = int(np.argmin(np.abs(x_display - event.x)))
        tx = t_rel[idx]
        vy_mm = float(mm[idx])
        hover_dot.set_data([tx], [vy_mm])
        lines_txt = [f"t = {fmt_elapsed(tx)}", f"{disp_label}: {vy_mm:.2f} mm"]
        if force_n is not None:
            vy_f = float(force_n[idx])
            lines_txt.append(f"{force_label}: {vy_f:.3f} N")
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
    plot_cycle(t, mm, force_n, meta, chosen["name"])


if __name__ == "__main__":
    main()
