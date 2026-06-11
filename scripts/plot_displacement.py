#!/usr/bin/env python3
"""Backward-compatible entry point; prefer ``spc-plot`` when installed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spc_reader.plot_live import main

if __name__ == "__main__":
    main()
