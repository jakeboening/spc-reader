# Force and displacement logger

Real-time dual-channel plot and HDF5 logger:

- **Mitutoyo Digimatic SPC** linear displacement (mm) on the 10-pin connector (USB-ITN, IT-016U, or serial bridge)
- **Mark-10 Series 5** force (N), e.g. M5-10 (10 lbf ≈ 44.5 N full scale) over USB virtual serial (GCL2)
- **RS485 load cell transmitter** force (N) via Modbus RTU (WTQ-style register map)

- 600 s scrolling window; displacement **0–30 mm**, force **±44.5 N** (defaults for M5-10)
- Hover tooltip on the live chart
- Each user’s log defaults to **`~/.local/share/spc-reader/force_and_displacement.h5`**

---

## macOS quick-start (load cell only)

No udev rules or `dialout` group needed — USB serial ports under `/dev/cu.*` are world-accessible. tkinter is optional: when it is missing (typical for Homebrew Python), the native `macosx` matplotlib backend is used automatically.

```bash
cd ~/src/spc-reader
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
spc-plot --list-ports          # find the RS485 adapter, e.g. /dev/cu.usbserial-B0009XBW
spc-plot --no-displacement --force-type loadcell \
    --force-port /dev/cu.usbserial-B0009XBW --loadcell-range 100kg
```

The adapter does not need to be plugged in before starting: in load cell mode the app starts with a "waiting for load cell" banner and connects automatically (it polls for the port twice a second). Unplugging mid-session is also handled — logging pauses and resumes when the adapter reappears.

Without a saved raw-ADC calibration for this port the force falls back to the transmitter's scaled register (possibly whole-kg resolution). For finer resolution run:

```bash
spc-loadcell-cal --port /dev/cu.usbserial-B0009XBW
```

---

## System install (all users)

Install the package and udev rules once as root. Each user runs `spc-plot` with their own data directory.

```bash
cd /path/to/spc-reader
sudo pip install .
sudo spc-reader-install-udev
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Add each user who will use serial devices to the **`dialout`** group, then log out and back in:

```bash
sudo usermod -aG dialout USERNAME
```

GUI (Fedora example):

```bash
sudo dnf install python3-tkinter python3-pillow-tk
```

`python3-pillow-tk` provides Pillow’s **ImageTk** bindings (toolbar icons on the plot window). Without it you may see an ImageTk warning; the chart still works and the warning is suppressed in recent `spc-reader` builds.

Replug USB devices after udev reload.

### Commands (after install)

| Command | Purpose |
|---------|---------|
| `spc-plot` | Live plot + log |
| `spc-plot-cycle` | List or replay logged cycles |
| `spc-reader-install-udev` | Install udev rules to `/etc/udev/rules.d/` |

### Per-user data

| Path | Contents |
|------|----------|
| `~/.local/share/spc-reader/` | Default log directory |
| `~/.local/share/spc-reader/force_and_displacement.h5` | Default HDF5 log |

Override with `$XDG_DATA_HOME/spc-reader/` if set. Use `--data-file` for a custom path.

---

## ImageTk warning on another user account

If `spc-plot` prints a Pillow/ImageTk warning but the window appears, install Tk support for that user’s Python environment and re-login:

```bash
sudo dnf install python3-tkinter python3-pillow-tk   # Fedora
```

Then reinstall or upgrade the package so matplotlib warning filters apply:

```bash
sudo pip install --upgrade .
```

---

## Development / git checkout

```bash
pip install -e .
./scripts/run-plot.sh --list-ports
```

Or without install:

```bash
pip install -r requirements.txt
./scripts/run-plot.sh
```

---

## Wiring

```
Gauge ── 10-pin SPC cable ── USB Input Tool (USB-ITN / IT-020U / …) ── PC USB
Mark-10 M5-10 ── micro USB ── PC USB  (gauge menu: Serial/USB → USB selected)
Load cell ── 4-wire ── RS485 transmitter ── USB-RS485 adapter ── PC USB
```

USB-ITN appears as USB `0fe7:4001` and is read via **pyusb** (not a tty). The Mark-10 exposes a virtual COM port (`/dev/ttyUSB*` or `/dev/ttyACM*`). An RS485 load cell transmitter connects through a USB-RS485 adapter on `/dev/ttyUSB*` (macOS: `/dev/cu.usbserial-*`).

On the Mark-10: open **Serial/USB Settings**, select **USB**, set baud to **115200** (match `--force-baud`) and **Numeric + Units** data format. The gauge must be on the main measurement screen (not a menu) for GCL2 commands.

On the load cell transmitter: set **Modbus RTU** on RS485 (typically **9600 8N1**, slave address **1**). Match `--loadcell-range` to the load cell full scale configured on the transmitter.

---

## Run

```bash
spc-plot --list-ports
spc-plot
spc-plot --port usb-itn:40006743 --tag "run 42"
spc-plot --force-port /dev/ttyUSB0
spc-plot --force-type loadcell --force-port /dev/ttyUSB0 --loadcell-range 100kg
spc-plot --no-force
spc-plot --no-displacement --force-type loadcell --force-port /dev/ttyUSB0 --loadcell-range 100kg
```

With the plot window focused:

- **t** — stop the current run (flushes the HDF5 cycle and freezes the chart); press **t** again to start a new run with a fresh cycle group and a cleared chart. Sample data streams to the HDF5 file continuously while running — stopping is never needed to save data.
- **s** — save the "Cycle label" text as metadata on the current run, without stopping. This is the **only** way the label is saved (Enter / clicking away / stopping a run never save it). The label text carries over between runs; a red `* unsaved` marker next to the field shows whenever the text differs from what is saved to the current run — including right after starting a new run, since the new cycle begins unlabeled.
- **q** — quit (flushes the log and closes the serial port cleanly; cmd+W works too).

**Load cell hot-plug:** with `--force-type loadcell` the RS485 adapter may be absent at startup or unplugged mid-run. The app shows a "waiting for load cell" banner and polls for `--force-port` every 0.5 s; when the adapter (re)appears it reconnects and logging resumes in the same cycle. While disconnected in force-only mode nothing is logged (no NaN filler rows), so a gap in the `time` dataset marks the outage.

| Flag | Default | Description |
|------|---------|-------------|
| `--port PATH` | auto | Mitutoyo: `usb-itn`, `usb-itn:SERIAL`, or `/dev/tty*` |
| `--force-port PATH` | auto | Mark-10 USB serial or RS485 adapter (`/dev/tty*`) |
| `--force-type` | `mark10` | `mark10` or `loadcell` (RS485 Modbus RTU) |
| `--force-baud` | see below | `115200` (Mark-10) or `9600` (load cell) |
| `--loadcell-range` | `100kg` | Full scale: `10kg` … `1000kg` (sets Y-axis and scaling metadata) |
| `--loadcell-addr` | `1` | Modbus slave address |
| `--loadcell-decimals` | auto | Override decimal places from transmitter |
| `--no-force` | off | Skip force channel |
| `--no-displacement` | off | Skip Mitutoyo displacement (force-only) |
| `--window SECS` | `600` | Rolling window |
| `--ymin` / `--ymax` | `0` / `30` | Displacement Y-axis (mm) |
| `--fmin` / `--fmax` | ±44.5 N | Force Y-axis (N); default is M5-10 full scale |
| `--hz` | `30` | Poll rate |
| `--data-file` | see above | HDF5 log |

Replay:

```bash
spc-plot-cycle
spc-plot-cycle last
```

Older logs (`displacement_log.h5`, `temperature_log.h5`) still work with `--data-file` (legacy dataset names for displacement-only files).

---

## HDF5 layout

```
cycles/000001/
  time              float64[]   Unix epoch (UTC)
  displacement_mm   float32[]   linear displacement (mm)
  force_n           float32[]   force (N), when a force sensor was connected
  force_counts      float64[]   raw ADC counts (registers 40015/16), load cell only
  attrs: tag, label, serial, hz, start_iso, units, channel_name
          force_serial, force_units, force_channel_name, force_capacity_n  (when force logged)
          force_counts_source, force_cal_source            (load cell only)
          force_cal_zero_counts, force_cal_counts_per_kg   (when raw calibration active)
```

`force_cal_source` records how `force_n` was derived: `raw-adc` (counts mapped
through the saved 2-point calibration, whose parameters are stored alongside) or
`scaled-register` (transmitter's own scaling; no calibration was loaded). Raw
counts are always logged for the load cell, so any cycle can be re-converted
after a later calibration.

---

## Protocols

**Mitutoyo:** host sends `1` + CR; adapter returns a text line (e.g. `01A+12.345`). Inch readings are converted to mm.

**Mark-10 GCL2:** host sends `?C` + CR for the real-time reading, or consumes auto-output lines. The gauge may send e.g. `0.42 N` or `0.094 lbF`; values are normalized to **N** for plot and log.

**RS485 load cell (Modbus RTU):** function 03 read of registers 40007–40014 (status, gross weight, divisions/unit). Weight is an integer with implied decimal places; sign comes from the status register. Values are converted to **N** using the transmitter unit (typically kg).
