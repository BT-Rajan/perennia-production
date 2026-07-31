
#!/usr/bin/env bash
###############################################################################
# Perennia Installer
# Ubuntu 24.04 + CloudPanel
###############################################################################

set -Eeuo pipefail

APP_NAME="web"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${APP_DIR}/venv"

DB_HOST="127.0.0.1"
DB_PORT="3306"
DB_NAME="web"
DB_USER="appuser"
DB_PASSWORD="Chennai#44"

SERVER_IP=$(hostname -I | awk '{print $1}')

###############################################################################

GREEN="\033[32m"
RED="\033[31m"
BLUE="\033[36m"
NC="\033[0m"

step() {
    echo
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}============================================================${NC}"
}

ok() {
    echo -e "${GREEN}✓ $1${NC}"
}

fail() {
    echo -e "${RED}✗ $1${NC}"
    exit 1
}

###############################################################################

clear

echo
echo "============================================================"
echo "        PERENNIA INSTALLER"
echo "============================================================"
echo
echo "Application : ${APP_NAME}"
echo "Directory   : ${APP_DIR}"
echo "Server IP   : ${SERVER_IP}"
echo

###############################################################################
step "1/9 Checking Requirements"

command -v python3 >/dev/null || fail "python3 not installed"
command -v pip3 >/dev/null || fail "pip3 not installed"
command -v pm2 >/dev/null || fail "PM2 not installed"

ok "Requirements verified"

###############################################################################
step "2/9 Creating Virtual Environment"

if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi

source "${VENV_DIR}/bin/activate"

python --version

ok "Virtual environment ready"

###############################################################################
step "3/9 Installing Python Packages"

python -m pip install --upgrade pip

if [ -f requirements.txt ]; then
    pip install -r requirements.txt
fi

ok "Dependencies installed"

###############################################################################
step "4/9 Creating .env"

if [ ! -f .env ]; then

    if [ -f .env.example ]; then
        cp .env.example .env
    else
        touch .env
    fi

fi

cat > .env <<EOF
DB_HOST=${DB_HOST}
DB_PORT=${DB_PORT}
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASSWORD=${DB_PASSWORD}
APP_HOST=${SERVER_IP}
EOF

ok ".env configured"

###############################################################################
step "5/9 Creating Directories"

mkdir -p logs
mkdir -p uploads
mkdir -p storage

ok "Directories created"

###############################################################################
step "6/9 Setting Permissions"

find . -type d -exec chmod 755 {} \;
find . -type f -exec chmod 644 {} \;

chmod +x *.sh 2>/dev/null || true

ok "Permissions updated"

###############################################################################
step "7/9 Starting PM2"

pm2 delete web >/dev/null 2>&1 || true

pm2 start \
"${VENV_DIR}/bin/python" \
--name web \
-- \
-m uvicorn app.main:app \
--host 127.0.0.1 \
--port 8000

pm2 save

ok "Application started"

###############################################################################
step "8/9 Enabling Startup"

pm2 startup systemd -u "$(whoami)" --hp "$HOME" >/dev/null 2>&1 || true
pm2 save

ok "PM2 startup configured"

###############################################################################
step "9/9 Status"

pm2 status

echo
echo "============================================================"
echo "INSTALLATION COMPLETED"
echo "============================================================"
echo
echo "Application : web"
echo "Database    : ${DB_NAME}"
echo "User        : ${DB_USER}"
echo "Server IP   : ${SERVER_IP}"
echo
echo "Commands"
echo "--------"
echo "pm2 status"
echo "pm2 logs web"
echo "pm2 restart web"
echo "pm2 stop web"
echo
echo "Application is listening on 127.0.0.1:8000"
echo "Configure CloudPanel/Nginx to proxy to this port."
echo
