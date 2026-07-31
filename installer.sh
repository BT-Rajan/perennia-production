#!/usr/bin/env bash
###############################################################################
# Perennia Installer
# Ubuntu 24.04 + CloudPanel
###############################################################################

set -Eeuo pipefail

APP_NAME="web"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${APP_DIR}/venv"

APP_HOST="127.0.0.1"
APP_PORT="8000"

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
echo "                PERENNIA INSTALLER"
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
command -v openssl >/dev/null || fail "openssl not installed"

ok "Requirements verified"

###############################################################################
step "2/9 Creating Virtual Environment"

if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
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
step "4/9 Generating Configuration"

SECRET_KEY=$(openssl rand -hex 32)
JWT_SECRET_KEY=$(openssl rand -hex 32)
ENCRYPTION_KEY=$(openssl rand -hex 32)

cat > .env <<EOF
#########################################
# Application
#########################################

APP_HOST=${APP_HOST}
APP_PORT=${APP_PORT}

#########################################
# Security
#########################################

SECRET_KEY=${SECRET_KEY}
JWT_SECRET_KEY=${JWT_SECRET_KEY}
ENCRYPTION_KEY=${ENCRYPTION_KEY}

#########################################
# Database
#########################################

DB_HOST=${DB_HOST}
DB_PORT=${DB_PORT}
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASSWORD=${DB_PASSWORD}

#########################################
# Environment
#########################################

ENVIRONMENT=production
DEBUG=false
EOF

ok ".env generated"

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
step "7/9 Starting Application"

pm2 delete "${APP_NAME}" >/dev/null 2>&1 || true

pm2 start \
"${VENV_DIR}/bin/python" \
--name "${APP_NAME}" \
-- \
-m uvicorn app.main:app \
--host "${APP_HOST}" \
--port "${APP_PORT}"

pm2 save

ok "Application started"

###############################################################################
step "8/9 Configuring Startup"

pm2 startup systemd -u "$(whoami)" --hp "$HOME" >/dev/null 2>&1 || true
pm2 save

ok "Startup configured"

###############################################################################
step "9/9 Installation Status"

pm2 status

echo
echo "============================================================"
echo "             INSTALLATION COMPLETED"
echo "============================================================"
echo
echo "Application : ${APP_NAME}"
echo "Directory   : ${APP_DIR}"
echo "Database    : ${DB_NAME}"
echo "DB User     : ${DB_USER}"
echo "Server IP   : ${SERVER_IP}"
echo "Host        : ${APP_HOST}"
echo "Port        : ${APP_PORT}"
echo
echo "Secret Keys"
echo "-----------"
echo "SECRET_KEY        : Generated"
echo "JWT_SECRET_KEY    : Generated"
echo "ENCRYPTION_KEY    : Generated"
echo
echo "Useful Commands"
echo "---------------"
echo "pm2 status"
echo "pm2 logs ${APP_NAME}"
echo "pm2 restart ${APP_NAME}"
echo "pm2 stop ${APP_NAME}"
echo
echo "Application is pinned to ${APP_HOST}:${APP_PORT}"
echo "Configure CloudPanel/Nginx to proxy to port ${APP_PORT}."
echo
