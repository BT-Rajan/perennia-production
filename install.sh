#!/usr/bin/env bash
# Perennia - install (macOS / Linux)
set -e
cd "$(dirname "$0")"

echo "============================================================"
echo " PERENNIA - INSTALL"
echo "============================================================"
echo
echo "This will:"
echo "  1. Check for Python 3"
echo "  2. Create a virtual environment (./venv)"
echo "  3. Install dependencies"
echo "  4. Generate the secrets Perennia needs to start"
echo
read -p "Press Enter to continue..." _

echo
echo "[1/4] Checking Python..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "  Python 3 was not found. Install it from https://www.python.org/downloads/"
    echo "  (or via your system package manager) and run this installer again."
    exit 1
fi
echo "  $(python3 --version) found"

echo
echo "[2/4] Creating virtual environment..."
if [ -d venv ]; then
    echo "  venv already exists, skipping."
else
    python3 -m venv venv
    echo "  Done."
fi

echo
echo "[3/4] Installing dependencies (this can take a few minutes)..."
venv/bin/python -m pip install --upgrade pip >/dev/null
venv/bin/python -m pip install -r requirements.txt
echo "  Done."

echo
echo "[4/4] Generating configuration and secrets..."
venv/bin/python scripts/setup_env.py

echo
echo "============================================================"
echo " INSTALL COMPLETE"
echo "============================================================"
echo
echo "Your admin login was saved to ADMIN_CREDENTIALS.txt in this folder."
echo "Keep it safe, then feel free to delete the file."
echo
echo "Next: run ./start.sh to launch Perennia."
echo "Once it's running, open http://localhost:8001/admin to log in"
echo "and add your Anthropic API key (it's encrypted automatically —"
echo "you never need to edit a file to set it)."
echo
