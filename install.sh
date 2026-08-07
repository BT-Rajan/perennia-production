#!/usr/bin/env bash
# Perennia - install (macOS / Linux)
cd "$(dirname "$0")"

echo "============================================================"
echo " PERENNIA - INSTALL"
echo "============================================================"
echo
echo "This will:"
echo "  1. Check for Python"
echo "  2. Create a virtual environment (./venv)"
echo "  3. Install dependencies"
echo "  4. Generate the secrets Perennia needs to start"
echo
read -p "Press Enter to continue..." _

echo
echo "[1/4] Checking Python..."
if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "  Python was not found on PATH."
    echo "  Install Python 3.10+ from https://www.python.org/downloads/"
    echo "  (or via your system package manager), then run this installer again."
    echo
    read -p "Press Enter to exit..." _
    exit 1
fi
echo "  $(python3 --version) found"

echo
echo "[2/4] Creating virtual environment..."
if [ -d venv ]; then
    echo "  venv already exists, skipping."
else
    if ! python3 -m venv venv; then
        echo "  Failed to create the virtual environment."
        read -p "Press Enter to exit..." _
        exit 1
    fi
    echo "  Done."
fi

echo
echo "[3/4] Installing dependencies (this can take a few minutes)..."
venv/bin/python -m pip install --upgrade pip >/dev/null
if ! venv/bin/python -m pip install -r requirements.txt; then
    echo
    echo "  Failed to install dependencies. Check your internet connection and try again."
    echo "  You can also try running with elevated permissions (e.g. sudo) if this looks"
    echo "  like a permissions error."
    echo
    read -p "Press Enter to exit..." _
    exit 1
fi
echo "  Done."

echo
echo "[4/4] Generating configuration and secrets..."
if ! venv/bin/python scripts/setup_env.py; then
    read -p "Press Enter to exit..." _
    exit 1
fi

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
read -p "Press Enter to exit..." _
