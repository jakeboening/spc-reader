#!/usr/bin/env python3
"""Real-time two-channel thermocouple plotter for THRMCPL1-2BBF97 via VirtualHub.

Connects to a running VirtualHub instance (default: 127.0.0.1:4443) and
plots temperature1 and temperature2 as a live scrolling chart using
matplotlib.  Every run appends a new *cycle* to an HDF5 log file.

Usage:
    python3 scripts/plot_temperature.py [options]

Options:
    --hub HOST:PORT      VirtualHub HTTPS address (default: 127.0.0.1:4443)
    --user USER          VirtualHub username (default: user)
    --password PASSWORD  VirtualHub password (default: !asml!asml)
    --serial SERIAL      Module serial (default: THRMCPL1-2BBF97)
    --window SECS        Rolling window width in seconds (default: 600)
    --ymin TEMP          Y-axis minimum °C (default: 20)
    --ymax TEMP          Y-axis maximum °C (default: 800)
    --hz HZ              Polling rate in Hz (default: 5)
    --ch1-name NAME      Label for temperature1 (default: "Ch 1")
    --ch2-name NAME      Label for temperature2 (default: "Ch 2")
    --tag TEXT           Free-text label for this recording cycle
    --data-file PATH     HDF5 log file (default: data/temperature_log.h5)

HDF5 layout
-----------
cycles/
  000001/
    time           float64[], Unix epoch seconds (UTC)
    temperature1   float32[], °C  (NaN when sensor offline)
    temperature2   float32[], °C  (NaN when sensor offline)
    attrs: tag, serial, hz, start_iso, ch1_name, ch2_name
"""

import argparse
import collections
import datetime
import os
import sys
import time

# Use TkAgg to avoid MESA-INTEL Vulkan "FINISHME" warnings from Qt/GTK backends.
os.environ.setdefault("MPLBACKEND", "TkAgg")

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.animation import FuncAnimation

try:
    import h5py
except ImportError:
    sys.exit("h5py not found. Run:  pip install h5py  (or: pip install -r requirements.txt)")

try:
    from yoctopuce.yocto_api import YAPI, YRefParam
    from yoctopuce.yocto_temperature import YTemperature
except ImportError:
    sys.exit(
        "yoctopuce package not found. Run:  pip install yoctopuce\n"
        "(or: pip install -r requirements.txt)"
    )

CH1_COLOR = "tomato"
CH2_COLOR = "dodgerblue"


# ── DataLogger ────────────────────────────────────────────────────────────────

class DataLogger:
    """Buffers and flushes two-channel (time, T1, T2) samples to HDF5."""

    FLUSH_EVERY = 25

    def __init__(self, filepath: str, serial: str, hz: float,
                 tag: str, ch1_name: str, ch2_name: str):
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        self._path = filepath
        self._buf_t:  list[float] = []
        self._buf_v1: list[float] = []
        self._buf_v2: list[float] = []

        with h5py.File(filepath, "a") as f:
            cycles = f.require_group("cycles")
            cycle_idx = len(cycles) + 1
            grp_name = f"{cycle_idx:06d}"
            grp = cycles.create_group(grp_name)
            grp.attrs["tag"]       = tag or ""
            grp.attrs["serial"]    = serial
            grp.attrs["hz"]        = hz
            grp.attrs["start_iso"] = datetime.datetime.now().isoformat()
            grp.attrs["ch1_name"]  = ch1_name
            grp.attrs["ch2_name"]  = ch2_name
            for ds_name, dtype in [("time", "f8"), ("temperature1", "f4"), ("temperature2", "f4")]:
                grp.create_dataset(
                    ds_name, shape=(0,), maxshape=(None,),
                    dtype=dtype, chunks=(256,), compression="gzip",
                )
            self._grp_path = f"cycles/{grp_name}"

        tag_display = f"  tag={tag!r}" if tag else ""
        print(f"Logging → {filepath}  [{self._grp_path}]{tag_display}")

    def append(self, unix_t: float, t1: float, t2: float) -> None:
        self._buf_t.append(unix_t)
        self._buf_v1.append(t1)
        self._buf_v2.append(t2)
        if len(self._buf_t) >= self.FLUSH_EVERY:
            self._flush()

    def close(self) -> None:
        self._flush()

    def _flush(self) -> None:
        if not self._buf_t:
            return
        t_data  = self._buf_t[:]
        v1_data = self._buf_v1[:]
        v2_data = self._buf_v2[:]
        self._buf_t.clear()
        self._buf_v1.clear()
        self._buf_v2.clear()
        with h5py.File(self._path, "a") as f:
            grp = f[self._grp_path]
            for ds_name, data in [("time", t_data),
                                   ("temperature1", v1_data),
                                   ("temperature2", v2_data)]:
                ds = grp[ds_name]
                n = len(ds)
                ds.resize(n + len(data), axis=0)
                ds[n:] = data


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Real-time two-channel Yocto-Thermocouple plotter with HDF5 logging",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--hub",       default="127.0.0.1:4443", metavar="HOST:PORT")
    p.add_argument("--user",      default="user")
    p.add_argument("--password",  default="!asml!asml")
    p.add_argument("--serial",    default="THRMCPL1-2BBF97")
    p.add_argument("--window",    type=float, default=600,  metavar="SECS")
    p.add_argument("--ymin",      type=float, default=20,   metavar="TEMP")
    p.add_argument("--ymax",      type=float, default=800,  metavar="TEMP")
    p.add_argument("--hz",        type=float, default=5,    metavar="HZ")
    p.add_argument("--ch1-name",  default="Ch 1", metavar="NAME",
                   help="Label for temperature1")
    p.add_argument("--ch2-name",  default="Ch 2", metavar="NAME",
                   help="Label for temperature2")
    p.add_argument("--tag",       default="",
                   help="Free-text label for this recording cycle")
    p.add_argument("--data-file", default="data/temperature_log.h5", metavar="PATH")
    return p.parse_args()


# ── Hub / sensors ─────────────────────────────────────────────────────────────

def connect_hub(hub_addr, user, password):
    YAPI.SetNetworkSecurityOptions(YAPI.NO_TRUSTED_CA_CHECK | YAPI.NO_HOSTNAME_CHECK)
    url = f"https://{user}:{password}@{hub_addr}"
    errmsg = YRefParam()
    if YAPI.RegisterHub(url, errmsg) != YAPI.SUCCESS:
        sys.exit(f"Cannot connect to VirtualHub at {hub_addr}: {errmsg.value}\n"
                 "Make sure VirtualHub is running and credentials are correct.")


def find_sensors(serial):
    errmsg = YRefParam()
    YAPI.UpdateDeviceList(errmsg)   # force discovery before checking isOnline()

    s1 = YTemperature.FindTemperature(f"{serial}.temperature1")
    if s1.isOnline():
        print(f"  Ch1: {serial}.temperature1 online")
    else:
        print(f"  Ch1: {serial}.temperature1 not online — will log NaN")

    s2 = YTemperature.FindTemperature(f"{serial}.temperature2")
    if s2.isOnline():
        print(f"  Ch2: {serial}.temperature2 online")
    else:
        print(f"  Ch2: {serial}.temperature2 not online — will log NaN")

    if not s1.isOnline() and not s2.isOnline():
        sys.exit(f"Neither {serial}.temperature1 nor {serial}.temperature2 is online.\n"
                 "Check that the device is plugged in and VirtualHub has claimed it.")

    return s1, s2


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    connect_hub(args.hub, args.user, args.password)
    sensor1, sensor2 = find_sensors(args.serial)

    if sensor1.isOnline():
        sensor1.set_reportFrequency(f"{int(args.hz)}/s")
    if sensor2.isOnline():
        sensor2.set_reportFrequency(f"{int(args.hz)}/s")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_file = (
        args.data_file if os.path.isabs(args.data_file)
        else os.path.join(repo_root, args.data_file)
    )
    logger = DataLogger(data_file, args.serial, args.hz, args.tag,
                        args.ch1_name, args.ch2_name)

    max_points = int(args.window * args.hz * 1.1)
    times  = collections.deque(maxlen=max_points)
    temps1 = collections.deque(maxlen=max_points)
    temps2 = collections.deque(maxlen=max_points)

    # ── Timed report callbacks ────────────────────────────────────────────────
    # The hardware fires these at exactly the configured Hz using its own
    # oscillator.  Timestamps come from the module clock (synced to VirtualHub
    # at connect time), so they're independent of Python timer jitter.

    # Pending reports queue: callbacks push, animation loop drains.
    pending_ch1: list[tuple[float, float]] = []  # (unix_t, °C)
    pending_ch2: list[tuple[float, float]] = []

    def on_ch1_report(_sensor, measure):
        t = measure.get_endTimeUTC()
        v = measure.get_averageValue()
        pending_ch1.append((t, v))

    def on_ch2_report(_sensor, measure):
        t = measure.get_endTimeUTC()
        v = measure.get_averageValue()
        pending_ch2.append((t, v))

    if sensor1.isOnline():
        sensor1.registerTimedReportCallback(on_ch1_report)
    if sensor2.isOnline():
        sensor2.registerTimedReportCallback(on_ch2_report)

    # ── Figure ────────────────────────────────────────────────────────────────

    fig, ax = plt.subplots(num="Yoctopuce Temperature Logger", figsize=(13, 6))
    fig.patch.set_facecolor("#F0F0F0")
    ax.set_facecolor("#F0F0F0")

    line1, = ax.plot([], [], color=CH1_COLOR,  linewidth=2, label=args.ch1_name)
    line2, = ax.plot([], [], color=CH2_COLOR,  linewidth=2, label=args.ch2_name)
    ax.legend(loc="upper right", fontsize=12)

    ax.set_ylim(min(args.ymin, 0), args.ymax)
    ax.set_ylabel("Temperature (°C)", fontsize=14, fontweight="bold")
    ax.set_xlabel(f"Time  ({int(args.window)} s rolling window)", fontsize=14, fontweight="bold")
    ax.set_title(args.tag if args.tag else "", fontsize=13)
    ax.yaxis.grid(True, color="#D2D2D2", linewidth=1)
    ax.xaxis.grid(False)
    ax.tick_params(labelsize=12)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    fig.autofmt_xdate()

    readout = ax.text(
        0.01, 0.97, "", transform=ax.transAxes,
        fontsize=12, verticalalignment="top", family="monospace",
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

    live = {"t": [], "v1": [], "v2": []}

    def on_hover(event):
        if event.inaxes is not ax or not live["t"]:
            hover_dot1.set_data([], [])
            hover_dot2.set_data([], [])
            hover_ann.set_visible(False)
            fig.canvas.draw_idle()
            return

        t_arr  = np.array(live["t"])
        v1_arr = np.array(live["v1"])
        v2_arr = np.array(live["v2"])

        # Use a finite reference row for pixel-space x lookup (either channel).
        ref_arr = v1_arr if not np.all(np.isnan(v1_arr)) else v2_arr
        x_display = ax.transData.transform(np.column_stack([t_arr, ref_arr]))[:, 0]
        idx = int(np.argmin(np.abs(x_display - event.x)))

        tx  = t_arr[idx]
        ty1 = v1_arr[idx]
        ty2 = v2_arr[idx]
        ts  = mdates.num2date(tx).strftime("%H:%M:%S.%f")[:-3]

        hover_ann.xy = (tx, ty1) if not np.isnan(ty1) else (tx, ty2)
        ty1_str = f"{ty1:.2f} °C" if not np.isnan(ty1) else "Disconnected"
        ty2_str = f"{ty2:.2f} °C" if not np.isnan(ty2) else "Disconnected"
        if not np.isnan(ty1):
            hover_dot1.set_data([tx], [ty1])
        else:
            hover_dot1.set_data([], [])
        if not np.isnan(ty2):
            hover_dot2.set_data([tx], [ty2])
        else:
            hover_dot2.set_data([], [])
        hover_ann.set_text(
            f"{ts}\n"
            f"{args.ch1_name}: {ty1_str}\n"
            f"{args.ch2_name}: {ty2_str}"
        )
        hover_ann.set_visible(True)

        ax_ext = ax.get_window_extent()
        x_frac = (x_display[idx] - ax_ext.x0) / ax_ext.width
        x_off = -8 if x_frac > 0.5 else 8
        hover_ann.xyann = (x_off, 8)
        hover_ann.set_ha("right" if x_off < 0 else "left")

        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("motion_notify_event", on_hover)

    # ── Animation loop ────────────────────────────────────────────────────────
    # The GUI timer runs at ~50 ms (20 fps) just for screen refresh.
    # It drains whatever hardware-timed reports have arrived since the last
    # frame — no polling, no missed/duplicate samples.

    GUI_INTERVAL_MS = 50
    _last_devlist_update = [0.0]
    DEVLIST_INTERVAL = 1.0

    # Unix timestamp of the last sample we successfully committed for each
    # channel.  Used for the grace-period logic below.
    _last_ch1_unix = [0.0]
    _last_ch2_unix = [0.0]

    def update(_frame):
        now = time.monotonic()
        if now - _last_devlist_update[0] >= DEVLIST_INTERVAL:
            errmsg = YRefParam()
            YAPI.UpdateDeviceList(errmsg)
            _last_devlist_update[0] = now
        YAPI.HandleEvents()

        # Drain reports from whichever channels have data.  When both are
        # active, pair ch2 to ch1 by nearest timestamp (they share the same
        # hardware tick but endTimeUTC can differ by a few milliseconds).
        # When only one channel has data this frame but the other was active
        # within the last two sample periods, defer the lone batch for one
        # more frame rather than logging NaN — this eliminates the
        # "Disconnected" flicker caused by callback timing jitter on
        # reconnect and normal inter-frame scheduling noise.
        if not pending_ch1 and not pending_ch2:
            return (line1, line2, readout)

        ch1_batch = pending_ch1[:]
        ch2_batch = pending_ch2[:]
        pending_ch1.clear()
        pending_ch2.clear()

        half_period = 0.5 / args.hz
        # Two sample periods is long enough to absorb any inter-frame jitter
        # while still declaring a channel offline promptly.
        grace = 2.0 / args.hz
        now_unix = time.time()

        if ch1_batch and ch2_batch:
            # Both channels reported this frame — both are definitively
            # connected.  Pair ch2 to ch1 by nearest timestamp; no distance
            # gate since we already know both are live (the half_period
            # constraint was causing false "Disconnected" on reconnect when
            # timestamps hadn't re-synced yet).
            _last_ch1_unix[0] = ch1_batch[-1][0]
            _last_ch2_unix[0] = ch2_batch[-1][0]
            ch2_times = [t for t, _ in ch2_batch]
            ch2_vals  = [v for _, v in ch2_batch]
            ch2_used  = [False] * len(ch2_batch)

            for t_unix, v1 in ch1_batch:
                diffs = [abs(t_unix - t2) for t2 in ch2_times]
                best  = int(np.argmin(diffs))
                if not ch2_used[best]:
                    v2 = ch2_vals[best]
                    ch2_used[best] = True
                else:
                    # All ch2 samples already claimed; carry forward the most
                    # recent ch2 value (channel is still alive).
                    v2 = ch2_vals[-1]

                t_mpl = mdates.date2num(datetime.datetime.fromtimestamp(t_unix))
                times.append(t_mpl)
                temps1.append(v1)
                temps2.append(v2)
                logger.append(t_unix, v1, v2)

        elif ch1_batch:
            if now_unix - _last_ch2_unix[0] < grace:
                # Ch2 was recently active; it probably just landed in a
                # different frame.  Put ch1 back and wait one more tick.
                pending_ch1[:0] = ch1_batch
                return (line1, line2, readout)
            _last_ch1_unix[0] = ch1_batch[-1][0]
            for t_unix, v1 in ch1_batch:
                t_mpl = mdates.date2num(datetime.datetime.fromtimestamp(t_unix))
                times.append(t_mpl)
                temps1.append(v1)
                temps2.append(float("nan"))
                logger.append(t_unix, v1, float("nan"))

        else:
            if now_unix - _last_ch1_unix[0] < grace:
                # Symmetric: defer ch2 until ch1 catches up.
                pending_ch2[:0] = ch2_batch
                return (line1, line2, readout)
            _last_ch2_unix[0] = ch2_batch[-1][0]
            for t_unix, v2 in ch2_batch:
                t_mpl = mdates.date2num(datetime.datetime.fromtimestamp(t_unix))
                times.append(t_mpl)
                temps1.append(float("nan"))
                temps2.append(v2)
                logger.append(t_unix, float("nan"), v2)

        if len(times) < 2:
            return (line1, line2, readout)

        now_mpl = mdates.date2num(datetime.datetime.now())
        cutoff = mdates.date2num(
            datetime.datetime.now() - datetime.timedelta(seconds=args.window)
        )
        vis_t  = [t for t in times if t >= cutoff]
        vis_v1 = [v for t, v in zip(times, temps1) if t >= cutoff]
        vis_v2 = [v for t, v in zip(times, temps2) if t >= cutoff]

        line1.set_data(vis_t, vis_v1)
        line2.set_data(vis_t, [v if not np.isnan(v) else None for v in vis_v2])
        live["t"]  = vis_t
        live["v1"] = vis_v1
        live["v2"] = vis_v2

        ax.set_xlim(
            mdates.date2num(
                datetime.datetime.now() - datetime.timedelta(seconds=args.window)
            ),
            now_mpl,
        )

        v1_last = temps1[-1]
        v2_last = temps2[-1]
        v1_str = f"{v1_last:.2f} °C" if not np.isnan(v1_last) else "Disconnected"
        v2_str = f"{v2_last:.2f} °C" if not np.isnan(v2_last) else "Disconnected"
        last_batch = ch1_batch if ch1_batch else ch2_batch
        t_last = datetime.datetime.fromtimestamp(last_batch[-1][0])
        readout.set_text(
            f"{args.ch1_name}: {v1_str}\n"
            f"{args.ch2_name}: {v2_str}\n"
            f"{t_last.strftime('%H:%M:%S.%f')[:-3]}"
        )
        return (line1, line2, readout)

    ani = FuncAnimation(  # noqa: F841
        fig, update, interval=GUI_INTERVAL_MS, blit=False, cache_frame_data=False
    )

    plt.tight_layout()
    try:
        plt.show()
    finally:
        logger.close()
        YAPI.FreeAPI()


if __name__ == "__main__":
    main()
