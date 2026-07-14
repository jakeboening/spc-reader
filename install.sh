#!/usr/bin/env bash
# spc-reader installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/jakeboening/spc-reader/main/install.sh | bash
#
# Installs into ~/.spc-reader (source checkout + private virtualenv) and links
# the command-line tools into ~/.local/bin. Safe to re-run: it updates an
# existing install in place.
#
# Overrides (mainly for testing): SPC_READER_HOME, SPC_READER_BIN.

set -euo pipefail

REPO_URL="https://github.com/jakeboening/spc-reader.git"
TARBALL_URL="https://codeload.github.com/jakeboening/spc-reader/tar.gz/refs/heads/main"
INSTALL_DIR="${SPC_READER_HOME:-$HOME/.spc-reader}"
SRC_DIR="$INSTALL_DIR/src"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="${SPC_READER_BIN:-$HOME/.local/bin}"
TOOLS=(spc-plot spc-plot-cycle spc-loadcell-probe spc-loadcell-cal spc-reader-install-udev)

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── Python 3.10+ ──────────────────────────────────────────────────────────────
PY=""
for cmd in python3 python; do
  command -v "$cmd" >/dev/null 2>&1 || continue
  if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$cmd"
    break
  fi
done
if [[ -z "$PY" ]]; then
  case "$(uname -s)" in
    Darwin) hint="Install it with:  brew install python3   (or from https://www.python.org/downloads/)" ;;
    *)      hint="Install it with your package manager, e.g.:  sudo apt install python3 python3-venv   or:  sudo dnf install python3" ;;
  esac
  die "Python 3.10 or newer not found. $hint  Then re-run this installer."
fi
info "Using $("$PY" --version 2>&1) ($(command -v "$PY"))"

# ── Fetch the source ──────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"
if command -v git >/dev/null 2>&1; then
  if [[ -d "$SRC_DIR/.git" ]]; then
    info "Updating source in $SRC_DIR"
    git -C "$SRC_DIR" fetch --depth 1 origin main
    git -C "$SRC_DIR" reset --hard origin/main --quiet
  else
    rm -rf "$SRC_DIR"
    info "Cloning $REPO_URL"
    git clone --depth 1 --quiet "$REPO_URL" "$SRC_DIR"
  fi
else
  info "git not found — downloading a source snapshot instead"
  rm -rf "$SRC_DIR"
  mkdir -p "$SRC_DIR"
  curl -fsSL "$TARBALL_URL" | tar -xz -C "$SRC_DIR" --strip-components=1
fi

# ── Virtualenv + package ──────────────────────────────────────────────────────
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  info "Creating virtual environment in $VENV_DIR"
  "$PY" -m venv "$VENV_DIR" || die "'$PY -m venv' failed. On Debian/Ubuntu install it with:  sudo apt install python3-venv"
fi
info "Installing spc-reader and its dependencies (may take a few minutes on first run)"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet --upgrade "$SRC_DIR"

# ── Command-line tools ────────────────────────────────────────────────────────
mkdir -p "$BIN_DIR"
for tool in "${TOOLS[@]}"; do
  [[ -x "$VENV_DIR/bin/$tool" ]] && ln -sf "$VENV_DIR/bin/$tool" "$BIN_DIR/$tool"
done
info "Linked tools into $BIN_DIR: ${TOOLS[*]}"

# ── PATH ──────────────────────────────────────────────────────────────────────
path_ok=false
case ":$PATH:" in
  *":$BIN_DIR:"*) path_ok=true ;;
esac
if ! $path_ok; then
  # Append to the login shell's rc file (marker keeps re-runs from duplicating).
  case "$(basename "${SHELL:-/bin/sh}")" in
    zsh)  rc="$HOME/.zshrc" ;;
    bash) rc="$HOME/.bashrc" ;;
    *)    rc="$HOME/.profile" ;;
  esac
  if ! grep -qs "spc-reader installer" "$rc"; then
    printf '\nexport PATH="%s:$PATH"  # added by spc-reader installer\n' "$BIN_DIR" >> "$rc"
    info "Added $BIN_DIR to PATH in $rc — open a new terminal (or run: source $rc)"
  else
    info "PATH entry already present in $rc — open a new terminal if commands aren't found"
  fi
fi

# ── Linux USB permissions ─────────────────────────────────────────────────────
if [[ "$(uname -s)" == "Linux" && ! -f /etc/udev/rules.d/99-mitutoyo-serial.rules ]]; then
  info "Linux: for USB device permissions, run once:"
  echo "        sudo $BIN_DIR/spc-reader-install-udev"
  echo "        sudo udevadm control --reload && sudo udevadm trigger    # then replug devices"
fi

echo
info "spc-reader installed. Try:"
echo "        spc-plot --list-ports"
echo "        spc-plot --mode temperature"
echo "        (re-run this installer anytime to update)"
