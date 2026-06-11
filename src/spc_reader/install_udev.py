"""Install udev rules system-wide (requires root)."""

from __future__ import annotations

import shutil
import sys
from importlib import resources
from pathlib import Path

RULE_NAMES = (
    "99-mitutoyo-serial.rules",
    "99-mark10-serial.rules",
)


def main() -> None:
    if not sys.platform.startswith("linux"):
        sys.exit("udev rules are only supported on Linux.")
    dest_dir = Path("/etc/udev/rules.d")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        sys.exit(f"Permission denied creating {dest_dir}. Run with sudo.")

    pkg = resources.files("spc_reader").joinpath("udev")
    for name in RULE_NAMES:
        src = pkg / name
        dest = dest_dir / name
        try:
            shutil.copy2(src, dest)
        except PermissionError:
            sys.exit(f"Permission denied writing {dest}. Run with sudo.")
        print(f"Installed {dest}")

    print("Run:  sudo udevadm control --reload-rules && sudo udevadm trigger")
    print("Then replug USB devices.")
