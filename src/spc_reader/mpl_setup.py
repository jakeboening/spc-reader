"""Configure matplotlib before pyplot import (backend + known Tk noise).

Import this module before ``matplotlib.pyplot`` in any GUI entry point::

    from spc_reader import mpl_setup  # noqa: F401
"""

from __future__ import annotations

import os
import warnings

os.environ.setdefault("MPLBACKEND", "TkAgg")

# Pillow 11.3+ / TkAgg (fixed upstream in matplotlib >= 3.10.4).
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r".*[Mm]ode.*parameter.*",
)
# Fedora etc. often ship Pillow without Tk bindings; plots still work (toolbar icons may not).
warnings.filterwarnings(
    "ignore",
    category=UserWarning,
    message=r".*ImageTk.*",
)
warnings.filterwarnings(
    "ignore",
    category=UserWarning,
    message=r".*Tk.*support.*",
)
