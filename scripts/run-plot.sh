#!/usr/bin/env bash
# Live displacement + force plotter (uses spc-plot when installed system-wide).
set -euo pipefail

export MPLBACKEND="${MPLBACKEND:-TkAgg}"

if command -v spc-plot >/dev/null 2>&1; then
    exec spc-plot "$@"
fi

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m spc_reader.plot_live "$@"
