# Force and displacement logger

Real-time dual-channel plot and HDF5 logger:

- **Mitutoyo Digimatic SPC** linear displacement (mm) on the 10-pin connector (USB-ITN, IT-016U, or serial bridge)
- **Mark-10 Series 5** force (N), e.g. M5-10 (10 lbf ≈ 44.5 N full scale) over USB virtual serial (GCL2)

- 600 s scrolling window; displacement **0–30 mm**, force **±44.5 N** (defaults for M5-10)
- Hover tooltip on the live chart
- Each user’s log defaults to **`~/.local/share/spc-reader/force_and_displacement.h5`**

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
```

USB-ITN appears as USB `0fe7:4001` and is read via **pyusb** (not a tty). The Mark-10 exposes a virtual COM port (`/dev/ttyUSB*` or `/dev/ttyACM*`).

On the Mark-10: open **Serial/USB Settings**, select **USB**, set baud to **115200** (match `--force-baud`) and **Numeric + Units** data format. The gauge must be on the main measurement screen (not a menu) for GCL2 commands.

---

## Run

```bash
spc-plot --list-ports
spc-plot
spc-plot --port usb-itn:40006743 --tag "run 42"
spc-plot --force-port /dev/ttyUSB0
spc-plot --no-force
```

With the plot window focused, press **r** to flush the current HDF5 cycle, start a new one, and clear the rolling chart.

| Flag | Default | Description |
|------|---------|-------------|
| `--port PATH` | auto | Mitutoyo: `usb-itn`, `usb-itn:SERIAL`, or `/dev/tty*` |
| `--force-port PATH` | auto | Mark-10 USB serial |
| `--force-baud` | `115200` | Mark-10 baud (must match gauge) |
| `--no-force` | off | Skip force channel |
| `--window SECS` | `600` | Rolling window |
| `--ymin` / `--ymax` | `0` / `30` | Displacement Y-axis (mm) |
| `--fmin` / `--fmax` | ±44.5 N | Force Y-axis (N); default is M5-10 full scale |
| `--hz` | `20` | Poll rate |
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
  force_n           float32[]   force (N), when Mark-10 was connected
  attrs: tag, serial, hz, start_iso, units, channel_name
          force_serial, force_units, force_channel_name  (when force logged)
```

---

## Protocols

**Mitutoyo:** host sends `1` + CR; adapter returns a text line (e.g. `01A+12.345`). Inch readings are converted to mm.

**Mark-10 GCL2:** host sends `?C` + CR for the real-time reading, or consumes auto-output lines. The gauge may send e.g. `0.42 N` or `0.094 lbF`; values are normalized to **N** for plot and log.
