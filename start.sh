#!/usr/bin/env bash
# Perennia - start (macOS / Linux)
set -e
cd "$(dirname "$0")"

if [ ! -x venv/bin/python ]; then
    echo "No virtual environment found. Run ./install.sh first."
    exit 1
fi

# Safety net: generate .env if install.sh was skipped or interrupted.
venv/bin/python scripts/setup_env.py

echo
echo "============================================================"
echo " Starting Perennia — press Ctrl+C to stop."
echo "============================================================"
echo

venv/bin/python scripts/run_server.py
