# Thermocouple live plotter — THRMCPL1-2BBF97

Real-time temperature plot and HDF5 data logger for the **Yocto-Thermocouple** (serial **THRMCPL1-2BBF97**) via **VirtualHub V2**.

- 600 s scrolling window, 20–800 °C Y-axis, 5 Hz sampling
- Hover tooltip snapping to nearest recorded sample
- Every run is saved as a numbered *cycle* in `data/temperature_log.h5` with an optional free-text tag

---

## Prerequisites

### Python packages

```bash
pip install -r requirements.txt
```

Packages: `yoctopuce`, `matplotlib`, `numpy`, `h5py`.

On Fedora, `tkinter` (the matplotlib GUI backend used here) is a separate system package:

```bash
sudo dnf install python3-tkinter
```

### VirtualHub V2

The `VirtualHubV2.linux.69177/` directory contains the pre-built binary. No installation needed — `run-plot.sh` starts it automatically.

### USB permissions

Copy the udev rule so VirtualHub can access the device without root:

```bash
sudo cp udev/51-yoctopuce.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

Replug the module after reloading udev.

---

## Run

```bash
./scripts/run-plot.sh
```

With a tag describing the test configuration:

```bash
./scripts/run-plot.sh --tag "substrate A, 300 W, 60 s dwell"
```

The tag appears as a subtitle on the plot and is stored in the HDF5 file.

`run-plot.sh` starts VirtualHub on port 4443 if nothing is already listening there, waits for it to be ready, then launches the plotter. VirtualHub is stopped on exit only if this script was the one that started it.

### All options

All flags are passed through from `run-plot.sh` (or `yoctotemp`) to `plot_temperature.py`.

| Flag | Default | Description |
|------|---------|-------------|
| `--hub HOST:PORT` | `127.0.0.1:4443` | VirtualHub HTTPS address |
| `--user USER` | `user` | VirtualHub username |
| `--password PW` | `!asml!asml` | VirtualHub password |
| `--serial SERIAL` | `THRMCPL1-2BBF97` | Module serial prefix |
| `--window SECS` | `600` | Rolling window width in seconds |
| `--ymin TEMP` | `20` | Y-axis minimum °C |
| `--ymax TEMP` | `800` | Y-axis maximum °C |
| `--hz HZ` | `5` | Polling rate in Hz (written to firmware) |
| `--ch1-name NAME` | `Ch 1` | Label for temperature1 in the legend and tooltip |
| `--ch2-name NAME` | `Ch 2` | Label for temperature2 in the legend and tooltip |
| `--tag TEXT` | *(empty)* | Free-text label stored with this cycle |
| `--data-file PATH` | `data/temperature_log.h5` | HDF5 log file |

Environment variables recognised by `run-plot.sh`:

| Variable | Default | Description |
|----------|---------|-------------|
| `VHUB_PORT` | `4443` | HTTPS port VirtualHub listens on (used by YAPI) |
| `VHUB_HTTP_PORT` | `4444` | Plain HTTP port VirtualHub listens on |
| `PYTHON` | `python3` | Python interpreter to use |

```bash
./scripts/run-plot.sh --window 300 --ymin 20 --ymax 500 --hz 1
./scripts/run-plot.sh --ch1-name "Top" --ch2-name "Bottom" --tag "run 42"
VHUB_PORT=4443 ./scripts/run-plot.sh   # if VirtualHub is on a non-default port
```

---

## HDF5 data log

Each run appends a new cycle to `data/temperature_log.h5` (gitignored).

```
cycles/
  000001/          ← first run
    time           float64[], Unix epoch seconds (UTC)
    temperature    float32[], °C
    attrs: tag, serial, hz, start_iso
  000002/          ← second run
    ...
```

### Reading back

```python
import h5py, numpy as np

with h5py.File("data/temperature_log.h5", "r") as f:
    for name, grp in f["cycles"].items():
        t = grp["time"][:]
        T = grp["temperature"][:]
        print(name, grp.attrs["tag"], f"{T.min():.1f}–{T.max():.1f} °C  n={len(T)}")
```

### Plotting a past cycle with `yoctotemp-plot`

`yoctotemp-plot` (or `./scripts/plot_cycle.py`) opens an interactive Matplotlib window for any saved cycle.

```bash
yoctotemp-plot          # list all recorded cycles
yoctotemp-plot 3        # plot cycle 3
yoctotemp-plot last     # plot the most recent cycle
```

| Argument | Default | Description |
|----------|---------|-------------|
| `cycle` *(positional)* | *(list all)* | Cycle number to plot, or `last` |
| `--data-file PATH` | `data/temperature_log.h5` | HDF5 log file to read from |

### Plotting a past cycle manually

```python
import h5py, numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timezone

with h5py.File("data/temperature_log.h5", "r") as f:
    grp = f["cycles/000001"]
    t = grp["time"][:]
    T = grp["temperature"][:]
    tag = grp.attrs["tag"]

dt = [datetime.fromtimestamp(ts, tz=timezone.utc).astimezone() for ts in t]

fig, ax = plt.subplots(figsize=(13, 5))
ax.plot(dt, T, color="tomato", linewidth=1.5)
ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
ax.set_ylabel("Temperature (°C)")
ax.set_title(f"Cycle 000001 — {tag}")
fig.autofmt_xdate()
plt.tight_layout()
plt.show()
```

---

## Sampling rate

The `--hz` value is written to the module's `reportFrequency` register on each launch and persists in firmware. The Yocto-Thermocouple supports up to **10/s**; the default is **5/s**.

## Standalone datalogger

The module can log to its own flash without a PC:

1. While connected, enable logging via VirtualHub (device → `temperature1` → **Sensor recording**) and set report frequency.
2. Sync the module clock via VirtualHub.
3. Power the board (USB host, charger, or YoctoHub) — it logs without a PC app running.
4. After the run, reconnect and download via VirtualHub or the Python API.

**Flash capacity at 5 Hz:** Yoctopuce guarantees ≥ 500 000 records. With one channel at 5 Hz that is ≈ 27.8 h before oldest samples are overwritten.
