#!/usr/bin/env bash
# ============================================================
#  LiteGate one-click launcher (Linux / macOS):
#  bootstrap deps -> frontend build check -> start gateway.
#  Stop the service with Ctrl+C. Requires Python 3.9+.
#  Usage: bash start.sh [--open] [--no-venv] [--reinstall]
#                       [--skip-build] [--listen HOST:PORT]
# ============================================================
set -u
cd "$(dirname "$0")" || exit 1

# ---- locate a Python 3.9+ interpreter (python3 preferred) ----
PY=""
if command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
fi
if [ -z "$PY" ]; then
  echo "[ERROR] Python not found. Install Python 3.9+ first:"
  echo "  Debian/Ubuntu : sudo apt install python3 python3-venv python3-pip"
  echo "  Fedora/RHEL   : sudo dnf install python3"
  echo "  macOS         : brew install python   (or: xcode-select --install)"
  exit 1
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "[ERROR] Python 3.9+ required. Current: $("$PY" -V 2>&1)"
  exit 1
fi

# ---- venv needs ensurepip (Debian/Ubuntu split it into python3-venv) ----
USE_VENV=1
for a in "$@"; do
  [ "$a" = "--no-venv" ] && USE_VENV=0
done
if [ "$USE_VENV" = "1" ] && ! "$PY" -c 'import ensurepip' 2>/dev/null; then
  echo "[ERROR] python3 venv/ensurepip module is missing. Install it with:"
  echo "  Debian/Ubuntu : sudo apt install python3-venv"
  echo "  Fedora/RHEL   : sudo dnf install python3-pip"
  echo "  or skip the virtualenv entirely: bash start.sh --no-venv"
  exit 1
fi

# ---- hand over to the cross-platform orchestrator ----
exec "$PY" scripts/start.py "$@"
