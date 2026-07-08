"""Per-user data paths (XDG Base Directory)."""

from __future__ import annotations

import os
from pathlib import Path

from .spc_io import DEFAULT_LOG_FILENAME

APP_NAME = "spc-reader"


def user_data_dir() -> Path:
    """``~/.local/share/spc-reader`` or ``$XDG_DATA_HOME/spc-reader``."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".local" / "share"
    return base / APP_NAME


def default_log_path() -> str:
    """Default HDF5 log in the current user's data directory."""
    d = user_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return str(d / DEFAULT_LOG_FILENAME)


def resolve_data_path(path: str) -> str:
    """Expand a log path.

    Absolute paths (and ``~``) are used as-is. A relative path is resolved
    against the current directory if it exists there or is written
    explicitly (``./`` or ``../``); otherwise it falls back to
    ``user_data_dir()``.
    """
    p = Path(path).expanduser()
    if p.is_absolute():
        return str(p)
    if p.exists() or path.startswith(("./", "../")):
        return str(p.resolve())
    return str(user_data_dir() / p)
