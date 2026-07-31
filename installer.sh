#!/usr/bin/env bash
###############################################################################
# Perennia Installer
# Ubuntu 24.04 + CloudPanel
#
# Safe to re-run: existing secrets in .env are preserved unless --force is
# passed, so re-running this to pick up a code update does NOT invalidate
# admin sessions, the admin password, or the encrypted LLM API key.
###############################################################################

set -Eeuo pipefail

APP_NAME="web"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${APP_DIR}/venv"
ENV_FILE="${APP_DIR}/.env"

cd "${APP_DIR}"

APP_HOST="127.0.0.1"
APP_PORT="8000"
ADMIN_USERNAME="admin"
ADMIN_PASSWORD=""
FORCE_REGEN=0

SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

###############################################################################

GREEN="\033[32m"
RED="\033[31m"
BLUE="\033[36m"
YELLOW="\033[33m"
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

warn() {
    echo -e "${YELLOW}! $1${NC}"
}

fail() {
    echo -e "${RED}✗ $1${NC}"
    exit 1
}

trap 'echo -e "${RED}Installer failed on line ${LINENO}. See the message above for details.${NC}"' ERR

###############################################################################
# Argument parsing
###############################################################################

usage() {
    cat <<EOF
Usage: ./installer.sh [options]

  --host HOST                App bind host (default: 127.0.0.1, only used on first install)
  --port PORT                App bind port (default: 8000, only used on first install)
  --admin-username NAME      Admin login username (default: admin, only used on first install)
  --admin-password PASS      Admin login password (default: auto-generated and printed once)
  --force                    Regenerate SECRET_KEY / ENCRYPTION_KEY / ADMIN_PASSWORD_HASH
                              even if .env already exists. This logs everyone out and
                              makes any previously-saved LLM API key unrecoverable.
  -h, --help                 Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) APP_HOST="$2"; shift 2 ;;
        --port) APP_PORT="$2"; shift 2 ;;
        --admin-username) ADMIN_USERNAME="$2"; shift 2 ;;
        --admin-password) ADMIN_PASSWORD="$2"; shift 2 ;;
        --force) FORCE_REGEN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1 (see --help)" ;;
    esac
done

###############################################################################

clear

echo
echo "============================================================"
echo "                PERENNIA INSTALLER"
echo "============================================================"
echo
echo "Application : ${APP_NAME}"
echo "Directory   : ${APP_DIR}"
echo "Server IP   : ${SERVER_IP:-unknown}"
echo

###############################################################################
step "1/8 Checking Requirements"

command -v python3 >/dev/null || fail "python3 not installed"
command -v pip3 >/dev/null || fail "pip3 not installed"
command -v pm2 >/dev/null || fail "PM2 not installed (npm install -g pm2)"

ok "Requirements verified"

###############################################################################
step "2/8 Creating Virtual Environment"

if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}" || fail "Could not create venv. On Ubuntu try: sudo apt install python3-venv"
fi

source "${VENV_DIR}/bin/activate"

python --version

ok "Virtual environment ready"

###############################################################################
step "3/8 Installing Python Packages"

python -m pip install --upgrade pip -q

if [ -f requirements.txt ]; then
    pip install -r requirements.txt -q
fi

ok "Dependencies installed"

###############################################################################
step "4/8 Generating Configuration (.env)"

# All three of the app's real required secrets -- SECRET_KEY (session/CSRF
# signing), ENCRYPTION_KEY (a valid Fernet key for encrypting the stored LLM
# API key), and ADMIN_PASSWORD_HASH (bcrypt) -- are generated here by the
# venv Python, which already has `cryptography` and `bcrypt` installed from
# step 3. Existing values are preserved on re-run unless --force is given.

GENERATED_PASSWORD_FILE="$(mktemp)"
trap 'rm -f "${GENERATED_PASSWORD_FILE}"' EXIT

FORCE_REGEN="${FORCE_REGEN}" \
ENV_FILE="${ENV_FILE}" \
IN_HOST="${APP_HOST}" \
IN_PORT="${APP_PORT}" \
IN_ADMIN_USERNAME="${ADMIN_USERNAME}" \
IN_ADMIN_PASSWORD="${ADMIN_PASSWORD}" \
OUT_PASSWORD_FILE="${GENERATED_PASSWORD_FILE}" \
python - <<'PYEOF'
import os
import secrets
import string

import bcrypt
from cryptography.fernet import Fernet

env_file = os.environ["ENV_FILE"]
force = os.environ.get("FORCE_REGEN") == "1"

existing = {}
if os.path.exists(env_file):
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            existing[k.strip()] = v.strip()

def get(key, default=""):
    return existing.get(key, default)

need_secret_key = force or not get("SECRET_KEY")
need_encryption_key = force or not get("ENCRYPTION_KEY")
need_admin_hash = force or not get("ADMIN_PASSWORD_HASH")

secret_key = secrets.token_urlsafe(48) if need_secret_key else get("SECRET_KEY")
encryption_key = Fernet.generate_key().decode() if need_encryption_key else get("ENCRYPTION_KEY")

generated_password = None
if need_admin_hash:
    admin_password = os.environ.get("IN_ADMIN_PASSWORD") or ""
    if not admin_password:
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
        admin_password = "".join(secrets.choice(alphabet) for _ in range(20))
        generated_password = admin_password
    admin_password_hash = bcrypt.hashpw(
        admin_password.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode()
else:
    admin_password_hash = get("ADMIN_PASSWORD_HASH")

host = get("HOST") or os.environ.get("IN_HOST", "127.0.0.1")
port = get("PORT") or os.environ.get("IN_PORT", "8000")
admin_username = get("ADMIN_USERNAME") or os.environ.get("IN_ADMIN_USERNAME", "admin")

# Anything else the app understands (see app/config.py + .env.example) is
# preserved verbatim if already present; otherwise the app's own built-in
# default is used by simply not being written into .env at all.
passthrough_keys = [
    "ALLOWED_ORIGINS", "COOKIE_SECURE", "SESSION_TTL_SECONDS",
    "RATE_LIMIT_CHAT", "RATE_LIMIT_LOGIN",
    "MAX_CHAT_EXCHANGES",
    "APPT_TIMEZONE", "APPT_SLOT_MINUTES", "APPT_DAY_START_HOUR",
    "APPT_DAY_END_HOUR", "APPT_WORKDAYS", "APPT_MAX_DAYS_AHEAD",
    "RATE_LIMIT_APPOINTMENT",
    "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_CALENDAR_ID",
]
passthrough = {k: existing[k] for k in passthrough_keys if k in existing}
cookie_secure = passthrough.pop("COOKIE_SECURE", "true")

lines = []
lines.append("#########################################")
lines.append("# Application")
lines.append("#########################################")
lines.append("")
lines.append(f"HOST={host}")
lines.append(f"PORT={port}")
lines.append(f"ALLOWED_ORIGINS={passthrough.pop('ALLOWED_ORIGINS', '')}")
lines.append("")
lines.append("#########################################")
lines.append("# Security")
lines.append("#########################################")
lines.append("")
lines.append(f"SECRET_KEY={secret_key}")
lines.append(f"ENCRYPTION_KEY={encryption_key}")
lines.append(f"ADMIN_USERNAME={admin_username}")
lines.append(f"ADMIN_PASSWORD_HASH={admin_password_hash}")
lines.append(f"COOKIE_SECURE={cookie_secure}")
lines.append(f"SESSION_TTL_SECONDS={passthrough.pop('SESSION_TTL_SECONDS', '3600')}")
lines.append("")
lines.append("#########################################")
lines.append("# Rate limits (per client IP)")
lines.append("#########################################")
lines.append("")
lines.append(f"RATE_LIMIT_CHAT={passthrough.pop('RATE_LIMIT_CHAT', '20/minute')}")
lines.append(f"RATE_LIMIT_LOGIN={passthrough.pop('RATE_LIMIT_LOGIN', '5/minute')}")
lines.append("")
lines.append("#########################################")
lines.append("# Chat session cap")
lines.append("#########################################")
lines.append("")
lines.append(f"MAX_CHAT_EXCHANGES={passthrough.pop('MAX_CHAT_EXCHANGES', '15')}")
lines.append("")
lines.append("#########################################")
lines.append("# Appointment booking")
lines.append("#########################################")
lines.append("")
lines.append(f"APPT_TIMEZONE={passthrough.pop('APPT_TIMEZONE', 'Asia/Kuwait')}")
lines.append(f"APPT_SLOT_MINUTES={passthrough.pop('APPT_SLOT_MINUTES', '30')}")
lines.append(f"APPT_DAY_START_HOUR={passthrough.pop('APPT_DAY_START_HOUR', '9')}")
lines.append(f"APPT_DAY_END_HOUR={passthrough.pop('APPT_DAY_END_HOUR', '17')}")
lines.append(f"APPT_WORKDAYS={passthrough.pop('APPT_WORKDAYS', '0,1,2,3,4')}")
lines.append(f"APPT_MAX_DAYS_AHEAD={passthrough.pop('APPT_MAX_DAYS_AHEAD', '30')}")
lines.append(f"RATE_LIMIT_APPOINTMENT={passthrough.pop('RATE_LIMIT_APPOINTMENT', '6/hour')}")
lines.append("")
lines.append("#########################################")
lines.append("# Google Calendar sync (optional)")
lines.append("#########################################")
lines.append("")
lines.append(f"GOOGLE_SERVICE_ACCOUNT_FILE={passthrough.pop('GOOGLE_SERVICE_ACCOUNT_FILE', '')}")
lines.append(f"GOOGLE_CALENDAR_ID={passthrough.pop('GOOGLE_CALENDAR_ID', '')}")
lines.append("")
lines.append("#########################################")
lines.append("# Environment")
lines.append("#########################################")
lines.append("")
lines.append("ENVIRONMENT=production")
lines.append("DEBUG=false")
lines.append("")

with open(env_file, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
os.chmod(env_file, 0o600)

if generated_password:
    with open(os.environ["OUT_PASSWORD_FILE"], "w", encoding="utf-8") as f:
        f.write(generated_password)
PYEOF

chmod 600 "${ENV_FILE}"
ok ".env written to ${ENV_FILE} (mode 600)"

if [ "${FORCE_REGEN}" = "1" ]; then
    warn "--force was set: SECRET_KEY, ENCRYPTION_KEY and ADMIN_PASSWORD_HASH were regenerated."
    warn "All admin sessions are now invalid, and any previously-saved LLM API key can no longer be decrypted."
fi

GENERATED_PASSWORD=""
if [ -s "${GENERATED_PASSWORD_FILE}" ]; then
    GENERATED_PASSWORD="$(cat "${GENERATED_PASSWORD_FILE}")"
fi
rm -f "${GENERATED_PASSWORD_FILE}"
trap - EXIT

###############################################################################
step "5/8 Creating Directories"

mkdir -p logs uploads storage data

ok "Directories created"

###############################################################################
step "6/8 Setting Permissions"

# Scoped on purpose: a recursive chmod across the whole app directory would
# also strip the executable bit from venv/bin/python and friends, which
# would break the app right after this script "successfully" creates it.
chmod +x ./*.sh 2>/dev/null || true
chmod 600 "${ENV_FILE}"
chmod 700 data logs uploads storage 2>/dev/null || true

ok "Permissions set (secrets + data private, venv untouched)"

###############################################################################
step "7/8 Starting Application"

pm2 delete "${APP_NAME}" >/dev/null 2>&1 || true

RUN_HOST="$(grep -E '^HOST=' "${ENV_FILE}" | cut -d= -f2-)"
RUN_PORT="$(grep -E '^PORT=' "${ENV_FILE}" | cut -d= -f2-)"

pm2 start \
"${VENV_DIR}/bin/python" \
--name "${APP_NAME}" \
--cwd "${APP_DIR}" \
-- \
-m uvicorn app.main:app \
--host "${RUN_HOST}" \
--port "${RUN_PORT}"

pm2 save

ok "Application started"

###############################################################################
step "8/8 Configuring Startup"

pm2 startup systemd -u "$(whoami)" --hp "$HOME" >/dev/null 2>&1 || true
pm2 save

ok "Startup configured"

echo
pm2 status

RUN_ADMIN_USER="$(grep -E '^ADMIN_USERNAME=' "${ENV_FILE}" | cut -d= -f2-)"

echo
echo "============================================================"
echo "             INSTALLATION COMPLETED"
echo "============================================================"
echo
echo "Application : ${APP_NAME}"
echo "Directory   : ${APP_DIR}"
echo "Server IP   : ${SERVER_IP:-unknown}"
echo "Host        : ${RUN_HOST}"
echo "Port        : ${RUN_PORT}"
echo
echo "Secret Keys (.env)"
echo "------------------"
echo "SECRET_KEY         : ready"
echo "ENCRYPTION_KEY      : ready (valid Fernet key)"
echo "ADMIN_PASSWORD_HASH : ready (bcrypt)"
echo
echo "Admin Login"
echo "-----------"
echo "URL      : http://${SERVER_IP:-$RUN_HOST}:${RUN_PORT}/admin (put CloudPanel/Nginx + TLS in front of this)"
echo "Username : ${RUN_ADMIN_USER}"
if [ -n "${GENERATED_PASSWORD}" ]; then
    echo -e "Password : ${YELLOW}${GENERATED_PASSWORD}${NC}  (auto-generated -- shown once, save it now)"
else
    echo "Password : unchanged from previous install (not shown). Use --force with --admin-password to reset it."
fi
echo
echo "Useful Commands"
echo "---------------"
echo "pm2 status"
echo "pm2 logs ${APP_NAME}"
echo "pm2 restart ${APP_NAME}"
echo "pm2 stop ${APP_NAME}"
echo
echo "Once TLS is live via CloudPanel/Nginx, keep COOKIE_SECURE=true in .env (it already is by default)."
echo
