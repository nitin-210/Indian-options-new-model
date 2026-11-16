#!/bin/bash
# Double-click (or: bash start-demo.command) to start the India Options dashboard.
# Uses Upstox (or Angel One) when its key is in config/secrets.toml, otherwise the demo.
cd "$(dirname "$0")" || exit 1
PY=""
for c in python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ] && [ ! -x .venv/bin/python ]; then
  echo "Python 3.11+ is needed. Install it from https://www.python.org/downloads/ (or: brew install python@3.12)."
  read -n 1 -s -r -p "Press any key to close"; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: creating .venv (takes a minute)..."
  "$PY" -m venv .venv || { read -n 1 -s -r -p "venv failed. Press any key"; exit 1; }
  .venv/bin/python -m pip install --upgrade pip -q
fi
# (re)install packages on the first run and whenever requirements.txt changes
if [ ! -f .venv/.req-installed ] || [ requirements.txt -nt .venv/.req-installed ]; then
  echo "Installing requirements..."
  .venv/bin/python -m pip install -r requirements.txt || { read -n 1 -s -r -p "pip install failed. Press any key"; exit 1; }
  touch .venv/.req-installed
fi
exec .venv/bin/python -m app "$@"
