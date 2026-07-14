#!/usr/bin/env python3
"""Plot a recorded cycle (displacement / force / temperature) from the HDF5 log.

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
from .spc_io import (
    DISPLACEMENT_DATASET,
    FORCE_DATASET,
    LBF_TO_N,
    TEMP1_DATASET,
    TEMP2_DATASET,
    read_force_n,
    read_temperatures_c,
    try_read_displacement_mm,
)

DISP_COLOR = "#eb6834"   # orange — displacement series (matches plot_live)
FORCE_COLOR = "#2a78d6"  # blue — force series (matches plot_live)
TEMP1_COLOR = "#9333ea"  # purple — thermocouple 1 (matches plot_live)
TEMP2_COLOR = "#0d9488"  # teal — thermocouple 2 (matches plot_live)


def load_channels(grp, meta: dict) -> list[dict]:
    """Channels present in a cycle group, in display order (first owns the
    left axis). Each: kind/unit/decimals/ylim (None = autoscale) and series
    as (dataset_key, label, color, array) tuples."""
    channels: list[dict] = []
    mm = try_read_displacement_mm(grp)
    if mm is not None and len(mm):
        channels.append({
            "kind": "displacement", "unit": "mm", "decimals": 2, "ylim": (0, 30),
            "series": [(
                DISPLACEMENT_DATASET,
                meta.get("channel_name", meta.get("ch1_name", "SPC")),
                DISP_COLOR, mm,
            )],
        })
    force_n = read_force_n(grp)
    if force_n is not None and len(force_n):
        cap = float(meta.get("force_capacity_n", 10 * LBF_TO_N))
        channels.append({
            "kind": "force", "unit": "N", "decimals": 3, "ylim": (-cap, cap),
            "series": [(
                FORCE_DATASET, meta.get("force_channel_name", "Force"),
                FORCE_COLOR, force_n,
            )],
        })
    t1, t2 = read_temperatures_c(grp)
    temp_series = []
    if t1 is not None and len(t1):
        temp_series.append((TEMP1_DATASET, meta.get("temp_ch1_name", "TC1"),
                            TEMP1_COLOR, t1))
    if t2 is not None and len(t2):
        temp_series.append((TEMP2_DATASET, meta.get("temp_ch2_name", "TC2"),
                            TEMP2_COLOR, t2))
    if temp_series:
        channels.append({
            # Autoscale on read-back — exploration beats fixed limits here.
            "kind": "temperature", "unit": "°C", "decimals": 1, "ylim": None,
            "series": temp_series,
        })
    return channels


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
            meta = {k: g.attrs[k] for k in g.attrs}
            channels = load_channels(g, meta)
            duration = t[-1] - t[0] if len(t) > 1 else 0
            entry = {
                "name":      name,
                "tag":       g.attrs.get("tag", ""),
                "serial":    g.attrs.get("serial", ""),
                "hz":        float(g.attrs.get("hz", 0)),
                "start_iso": g.attrs.get("start_iso", ""),
                "n":         len(t),
                "duration":  duration,
                "kinds":     {c["kind"] for c in channels},
            }
            for c in channels:
                allv = np.concatenate([arr for _k, _l, _c, arr in c["series"]])
                valid = allv[~np.isnan(allv.astype(float))]
                if len(valid):
                    entry[f"{c['kind']}_min"] = float(valid.min())
                    entry[f"{c['kind']}_max"] = float(valid.max())
            cycles.append(entry)
    return cycles


# Per-kind list column layout: (kind, unit, width, decimals).
_LIST_COLUMNS = (
    ("displacement", "mm", 7, 2),
    ("force", "N", 8, 3),
    ("temperature", "°C", 7, 1),
)


def list_cycles(cycles: list[dict]) -> None:
    cols = [c for c in _LIST_COLUMNS
            if any(c[0] in cyc["kinds"] for cyc in cycles)]
    header = f"{'#':>6}  {'Start':>19}  {'Dur (s)':>8}"
    for kind, unit, w, _d in cols:
        header += f"  {'Min ' + unit:>{w}}  {'Max ' + unit:>{w}}"
    header += f"  {'n':>6}  Tag"
    print(header)
    print("-" * len(header))
    for c in cycles:
        idx = int(c["name"])
        start = c["start_iso"][:19].replace("T", " ") if c["start_iso"] else "?"
        tag = c["tag"] or "—"
        row = f"{idx:>6}  {start:>19}  {c['duration']:>8.1f}"
        for kind, _unit, w, d in cols:
            vmin = c.get(f"{kind}_min")
            vmax = c.get(f"{kind}_max")
            row += (f"  {vmin:>{w}.{d}f}  {vmax:>{w}.{d}f}"
                    if vmin is not None else f"  {'—':>{w}}  {'—':>{w}}")
        row += f"  {c['n']:>6}  {tag}"
        print(row)


def load_data(path: str, name: str):
    with h5py.File(path, "r") as f:
        grp = f[f"cycles/{name}"]
        t = grp["time"][:]
        meta = {k: grp.attrs[k] for k in grp.attrs}
        channels = load_channels(grp, meta)
    return t, channels, meta


def export_cycle_csv(
    t: np.ndarray,
    channels: list[dict],
    meta: dict,
    name: str,
    out: str | None = None,
) -> str:
    """Write one cycle's time-series data to a CSV file.

    Parameters
    ----------
    t:        Unix epoch timestamps (float64).
    channels: Channel dicts from ``load_channels``.
    meta:     HDF5 group attributes dict.
    name:     Zero-padded HDF5 group name, e.g. ``"000003"``.
    out:      Output path.  Defaults to ``cycle_<N>.csv`` in the current directory.

    Returns
    -------
    str
        Absolute path of the written file.
    """
    cycle_num = int(name)
    if out is None:
        out = f"cycle_{cycle_num}.csv"
    out = os.path.abspath(out)

    kinds = {c["kind"] for c in channels}

    with open(out, "w", newline="") as fh:
        # Metadata comment header
        fh.write(f"# cycle: {cycle_num}\n")
        fh.write(f"# tag: {meta.get('tag', '')}\n")
        fh.write(f"# start: {meta.get('start_iso', '')}\n")
        if "displacement" in kinds:
            fh.write(f"# serial: {meta.get('serial', '')}\n")
            fh.write(f"# displacement_units: {meta.get('units', 'mm')}\n")
        fh.write(f"# hz: {meta.get('hz', '')}\n")
        if "force" in kinds:
            fh.write(f"# force_serial: {meta.get('force_serial', '')}\n")
            fh.write(f"# force_units: {meta.get('force_units', 'N')}\n")
        if "temperature" in kinds:
            fh.write(f"# temp_serial: {meta.get('temp_serial', '')}\n")
            fh.write(f"# temp_units: {meta.get('temp_units', 'C')}\n")

        columns = [(key, arr) for c in channels
                   for key, _label, _color, arr in c["series"]]
        writer = csv.DictWriter(fh, fieldnames=["time_unix", "elapsed_s",
                                                *(key for key, _a in columns)])
        writer.writeheader()

        t0 = float(t[0]) if len(t) else 0.0
        for i in range(len(t)):
            row: dict = {
                "time_unix": f"{t[i]:.6f}",
                "elapsed_s": f"{t[i] - t0:.6f}",
            }
            for key, arr in columns:
                row[key] = f"{arr[i]:.6f}"
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
    channels: list[dict],
    meta: dict,
    name: str,
) -> None:
    t_rel = t_unix - t_unix[0]
    duration_s = t_rel[-1] if len(t_rel) > 1 else 0

    tag = meta.get("tag", "")
    start = meta.get("start_iso", "")[:19].replace("T", " ")

    title = " + ".join(c["kind"] for c in channels).capitalize() + " cycle"
    fig, ax_main = plt.subplots(num=title, figsize=(13, 6))
    fig.patch.set_facecolor("#F0F0F0")
    ax_main.set_facecolor("#F0F0F0")

    legend_lines = []
    legend_labels = []
    for i, chn in enumerate(channels):
        ax = ax_main if i == 0 else ax_main.twinx()
        if i == 2:
            # Third axis also sits on the right; offset its spine outward.
            ax.spines["right"].set_position(("outward", 55))
        chn["ax"] = ax
        for _key, label, color, arr in chn["series"]:
            ln, = ax.plot(t_rel, arr, color=color, linewidth=1.5, label=label)
            legend_lines.append(ln)
            legend_labels.append(label)
        if chn["ylim"] is not None:
            ax.set_ylim(*chn["ylim"])
        axis_color = chn["series"][0][2]
        ax.set_ylabel(chn["unit"], fontsize=17, fontweight="bold", color=axis_color)
        ax.tick_params(axis="y", labelcolor=axis_color)

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
    for chn in channels:
        d = chn["decimals"]
        for _key, label, _color, arr in chn["series"]:
            valid = arr[~np.isnan(arr.astype(float))]
            if len(valid):
                stats_lines.append(
                    f"{label}:  min {valid.min():.{d}f}  max {valid.max():.{d}f}"
                    f"  mean {valid.mean():.{d}f} {chn['unit']}  n={len(valid):,}"
                )
            else:
                stats_lines.append(f"{label}: no data")
    ax_main.text(
        0.01, 0.97, "\n".join(stats_lines), transform=ax_main.transAxes,
        fontsize=13, verticalalignment="top", family="monospace",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.82, edgecolor="black"),
    )

    hover_color = channels[0]["series"][0][2]
    hover_dot, = ax_main.plot([], [], "o", color=hover_color, markersize=7, zorder=5)
    hover_ann = ax_main.annotate(
        "", xy=(0, 0), xytext=(8, 8), textcoords="offset points",
        fontsize=14,
        bbox=dict(boxstyle="round,pad=0.4", facecolor="white", edgecolor="gray", alpha=0.9),
        visible=False,
    )

    def on_hover(event):
        hover_axes = tuple(c["ax"] for c in channels)
        if event.inaxes not in hover_axes:
            hover_dot.set_data([], [])
            hover_ann.set_visible(False)
            fig.canvas.draw_idle()
            return

        # Dot/annotation anchor to the first channel's first series.
        hover_ax = channels[0]["ax"]
        y_arr = channels[0]["series"][0][3]

        x_display = hover_ax.transData.transform(np.column_stack([t_rel, y_arr]))[:, 0]
        idx = int(np.argmin(np.abs(x_display - event.x)))
        tx = t_rel[idx]
        vy = float(y_arr[idx])
        lines_txt = [f"t = {fmt_elapsed(tx)}"]
        for chn in channels:
            for _key, label, _color, arr in chn["series"]:
                lines_txt.append(
                    f"{label}: {float(arr[idx]):.{chn['decimals']}f} {chn['unit']}"
                )
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
    if len(channels) > 2:
        # Room for the third axis's outward-offset spine.
        fig.subplots_adjust(right=0.88)
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
    t, channels, meta = load_data(data_file, chosen["name"])
    if not channels:
        sys.exit(f"Cycle {int(chosen['name'])} contains no known data channels.")

    if args.csv is not None:
        out_path = args.csv if args.csv else None
        written = export_cycle_csv(t, channels, meta, chosen["name"], out=out_path)
        print(f"Exported cycle {int(chosen['name'])} → {written}")
    else:
        plot_cycle(t, channels, meta, chosen["name"])


if __name__ == "__main__":
    main()
