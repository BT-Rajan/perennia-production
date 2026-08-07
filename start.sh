#!/usr/bin/env bash
# Perennia - start (macOS / Linux)
# Runs the app under pm2 as a supervised process named "web" — pm2
# restarts it on crash, and (with `pm2 startup` + `pm2 save`, run once)
# can bring it back up automatically on system boot.
cd "$(dirname "$0")"

if [ ! -x venv/bin/python ]; then
    echo "No virtual environment found. Run ./install.sh first."
    exit 1
fi

# Safety net: generate .env if install.sh was skipped or interrupted.
venv/bin/python scripts/setup_env.py

if ! command -v pm2 >/dev/null 2>&1; then
    echo
    echo "pm2 was not found on PATH."
    echo "Install it with:  npm install -g pm2"
    echo "(requires Node.js/npm — https://nodejs.org)"
    echo
    exit 1
fi

echo
echo "============================================================"
echo " Starting Perennia under pm2 (process name: web)"
echo "============================================================"
echo

# startOrReload is idempotent: starts "web" if it isn't running yet,
# or reloads it in place if it already is — safe to run this script
# repeatedly (e.g. after a git pull) without ending up with duplicate
# or orphaned processes.
pm2 startOrReload ecosystem.config.js --update-env

echo
pm2 status web
echo
echo "Perennia is running under pm2 as 'web'."
echo "  View logs:   pm2 logs web"
echo "  Stop it:     pm2 stop web"
echo "  Restart it:  pm2 restart web"
echo "  Remove it:   pm2 delete web"
echo
echo "To have pm2 relaunch Perennia automatically after a system reboot"
echo "(only needs to be done once on this machine):"
echo "  pm2 startup"
echo "  pm2 save"
echo
