#!/usr/bin/env python3
"""Plot a recorded heating cycle from the HDF5 log.

Usage
-----
    # list all cycles
    python3 scripts/plot_cycle.py

    # plot cycle 3
    python3 scripts/plot_cycle.py 3

    # plot the most recent cycle
    python3 scripts/plot_cycle.py last

    # specify a different log file
    python3 scripts/plot_cycle.py --data-file /path/to/log.h5 3
"""

import argparse
import os
import sys

# Use TkAgg to avoid MESA-INTEL Vulkan "FINISHME" warnings from Qt/GTK backends.
os.environ.setdefault("MPLBACKEND", "TkAgg")

import h5py
import numpy as np
import matplotlib.pyplot as plt


# ── helpers ───────────────────────────────────────────────────────────────────

def default_data_file():
    real = os.path.realpath(__file__)   # resolve symlink before computing repo root
    repo_root = os.path.dirname(os.path.dirname(real))
    return os.path.join(repo_root, "data", "temperature_log.h5")


def load_cycles(path: str) -> list[dict]:
    """Return a list of cycle metadata dicts, sorted by index."""
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
            T = g["temperature1"][:] if "temperature1" in g else g["temperature"][:]
            duration = t[-1] - t[0] if len(t) > 1 else 0
            cycles.append({
                "name":      name,
                "tag":       g.attrs.get("tag", ""),
                "serial":    g.attrs.get("serial", ""),
                "hz":        float(g.attrs.get("hz", 0)),
                "start_iso": g.attrs.get("start_iso", ""),
                "n":         len(t),
                "duration":  duration,
                "t_min":     float(T.min()) if len(T) else float("nan"),
                "t_max":     float(T.max()) if len(T) else float("nan"),
                "t_mean":    float(T.mean()) if len(T) else float("nan"),
            })
    return cycles


def list_cycles(cycles: list[dict]) -> None:
    w_tag = max((len(c["tag"]) for c in cycles), default=0)
    w_tag = max(w_tag, 3)
    header = f"{'#':>6}  {'Start':>19}  {'Dur (s)':>8}  {'Min °C':>7}  {'Max °C':>7}  {'Mean °C':>8}  {'n':>6}  Tag"
    print(header)
    print("-" * len(header))
    for c in cycles:
        idx = int(c["name"])
        start = c["start_iso"][:19].replace("T", " ") if c["start_iso"] else "?"
        tag = c["tag"] or "—"
        print(
            f"{idx:>6}  {start:>19}  {c['duration']:>8.1f}"
            f"  {c['t_min']:>7.2f}  {c['t_max']:>7.2f}  {c['t_mean']:>8.2f}"
            f"  {c['n']:>6}  {tag}"
        )


def load_data(path: str, name: str):
    with h5py.File(path, "r") as f:
        grp = f[f"cycles/{name}"]
        t  = grp["time"][:]
        # Support both old single-channel files and new two-channel files.
        T1 = grp["temperature1"][:] if "temperature1" in grp else grp["temperature"][:]
        T2 = grp["temperature2"][:] if "temperature2" in grp else None
        meta = {k: grp.attrs[k] for k in grp.attrs}
    return t, T1, T2, meta


# ── plot ──────────────────────────────────────────────────────────────────────

def fmt_elapsed(seconds: float) -> str:
    """Format elapsed seconds as m:ss.d or h:mm:ss."""
    s = abs(seconds)
    if s < 3600:
        m, rem = divmod(s, 60)
        return f"{int(m)}:{rem:04.1f}"
    else:
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{int(h)}:{int(m):02d}:{sec:04.1f}"


CH1_COLOR = "tomato"
CH2_COLOR = "dodgerblue"


def plot_cycle(t_unix: np.ndarray, T1: np.ndarray, T2: np.ndarray | None,
               meta: dict, name: str) -> None:
    t_rel      = t_unix - t_unix[0]
    duration_s = t_rel[-1] if len(t_rel) > 1 else 0

    tag      = meta.get("tag", "")
    start    = meta.get("start_iso", "")[:19].replace("T", " ")
    ch1_name = meta.get("ch1_name", "Ch 1")
    ch2_name = meta.get("ch2_name", "Ch 2")
    has_ch2  = T2 is not None and not np.all(np.isnan(T2.astype(float)))

    fig, ax = plt.subplots(num="Temperature Cycle Analyzer", figsize=(13, 6))
    fig.patch.set_facecolor("#F0F0F0")
    ax.set_facecolor("#F0F0F0")

    line1, = ax.plot(t_rel, T1, color=CH1_COLOR, linewidth=1.5, label=ch1_name)
    if has_ch2:
        line2, = ax.plot(t_rel, T2, color=CH2_COLOR, linewidth=1.5, label=ch2_name)
        ax.legend(loc="upper right", fontsize=12)

    # ── X axis ────────────────────────────────────────────────────────────────

    nice = [1, 2, 5, 10, 15, 20, 30, 60, 120, 180, 300, 600, 900, 1800, 3600]
    tick_interval = min(nice, key=lambda x: abs(x - max(duration_s, 1) / 8))
    ax.xaxis.set_major_locator(plt.MultipleLocator(tick_interval))
    if tick_interval < 60:
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f} s"))
    else:
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: fmt_elapsed(x)))

    unit = "s" if tick_interval < 60 else "m:ss"
    ax.set_xlabel(f"Elapsed time ({unit})", fontsize=14, fontweight="bold")
    ax.set_ylabel("Temperature (°C)", fontsize=14, fontweight="bold")
    ax.yaxis.grid(True, color="#D2D2D2", linewidth=1)
    ax.xaxis.grid(False)
    ax.tick_params(labelsize=12)

    title_lines = [f"Cycle {int(name)} — started {start}   (duration {fmt_elapsed(duration_s)})"]
    if tag:
        title_lines.append(tag)
    ax.set_title("\n".join(title_lines), fontsize=13)

    # ── Stats box ─────────────────────────────────────────────────────────────

    def stats_line(label, arr, color):
        a = arr[~np.isnan(arr.astype(float))]
        if len(a) == 0:
            return f"{label}: no data"
        return (f"{label}:  min {a.min():.2f}  max {a.max():.2f}"
                f"  mean {a.mean():.2f} °C  n={len(a):,}")

    stats_text = stats_line(ch1_name, T1, CH1_COLOR)
    if has_ch2:
        stats_text += "\n" + stats_line(ch2_name, T2, CH2_COLOR)

    ax.text(
        0.01, 0.97, stats_text, transform=ax.transAxes,
        fontsize=11, verticalalignment="top", family="monospace",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.82, edgecolor="black"),
    )

    # ── Hover tooltip ─────────────────────────────────────────────────────────

    hover_dot1, = ax.plot([], [], "o", color=CH1_COLOR, markersize=7, zorder=5)
    hover_dot2, = ax.plot([], [], "o", color=CH2_COLOR, markersize=7, zorder=5)
    hover_ann = ax.annotate(
        "", xy=(0, 0), xytext=(8, 8), textcoords="offset points",
        fontsize=12,
        bbox=dict(boxstyle="round,pad=0.4", facecolor="white", edgecolor="gray", alpha=0.9),
        visible=False,
    )

    def on_hover(event):
        if event.inaxes is not ax:
            hover_dot1.set_data([], [])
            hover_dot2.set_data([], [])
            hover_ann.set_visible(False)
            fig.canvas.draw_idle()
            return

        x_display = ax.transData.transform(np.column_stack([t_rel, T1]))[:, 0]
        idx = int(np.argmin(np.abs(x_display - event.x)))

        tx  = t_rel[idx]
        ty1 = float(T1[idx])
        ty2 = float(T2[idx]) if has_ch2 else float("nan")

        hover_dot1.set_data([tx], [ty1])
        if has_ch2:
            hover_dot2.set_data([tx], [ty2])

        hover_ann.xy = (tx, ty1)
        v2_str = f"{ty2:.2f}" if not np.isnan(ty2) else "—"
        if has_ch2:
            hover_ann.set_text(
                f"t = {fmt_elapsed(tx)}\n"
                f"{ch1_name}: {ty1:.2f} °C\n"
                f"{ch2_name}: {v2_str} °C"
            )
        else:
            hover_ann.set_text(f"t = {fmt_elapsed(tx)}\n{ty1:.2f} °C")
        hover_ann.set_visible(True)

        ax_ext = ax.get_window_extent()
        x_frac = (x_display[idx] - ax_ext.x0) / ax_ext.width
        x_off = -8 if x_frac > 0.5 else 8
        hover_ann.xyann = (x_off, 8)
        hover_ann.set_ha("right" if x_off < 0 else "left")

        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("motion_notify_event", on_hover)

    plt.tight_layout()
    plt.show()


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Plot a recorded heating cycle from the HDF5 log.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "cycle", nargs="?", default=None,
        help="Cycle number to plot, or 'last'. Omit to list all cycles.",
    )
    p.add_argument(
        "--data-file", default=default_data_file(), metavar="PATH",
        help="HDF5 log file (default: data/temperature_log.h5)",
    )
    return p.parse_args()


def main():
    args = parse_args()
    cycles = load_cycles(args.data_file)

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

    t, T1, T2, meta = load_data(args.data_file, chosen["name"])
    plot_cycle(t, T1, T2, meta, chosen["name"])


if __name__ == "__main__":
    main()
