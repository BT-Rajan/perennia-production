"""
Application settings, loaded from environment variables (.env in dev).
No secret ever has a hardcoded default that would work in production —
anything security-sensitive that's missing causes a startup failure
instead of silently running insecurely.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"FATAL: required environment variable {name} is not set. "
              f"See .env.example and scripts/gen_secrets.py.", file=sys.stderr)
        sys.exit(1)
    return val


def _validate_timezone(tz_name: str) -> str:
    """Validate that a timezone string is valid.
    
    Raises sys.exit(1) if timezone is invalid.
    Returns the validated timezone name.
    """
    if not tz_name:
        print("FATAL: APPT_TIMEZONE is empty", file=sys.stderr)
        sys.exit(1)
    
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz_name)
        return tz_name
    except ValueError as e:
        print(
            f"FATAL: Invalid timezone '{tz_name}'. Error: {e}\n"
            "Please set APPT_TIMEZONE to a valid IANA timezone (e.g., 'America/New_York', 'Asia/Kuwait')\n"
            "See https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
            file=sys.stderr
        )
        sys.exit(1)


class Settings:
    # Where config.json / knowledge_base.json live. MUST be outside of
    # any directory served as static files.
    DATA_DIR: Path = Path(_get("DATA_DIR", str(BASE_DIR / "data")))

    # Directory of static, public assets (site HTML, images). Nothing
    # secret is ever allowed to live under here.
    PUBLIC_DIR: Path = Path(_get("PUBLIC_DIR", str(BASE_DIR / "public")))

    PORT: int = int(_get("PORT", "8000"))
    HOST: str = _get("HOST", "127.0.0.1")

    # Comma-separated list of origins allowed to call the API. Same-origin
    # browser requests don't need this, but keep it explicit rather than "*".
    ALLOWED_ORIGINS: list[str] = [
        o.strip() for o in _get("ALLOWED_ORIGINS", "").split(",") if o.strip()
    ]

    # Used to sign session cookies and CSRF tokens (itsdangerous).
    SECRET_KEY: str = _get("SECRET_KEY", required=True)

    # Used to encrypt the LLM API key at rest (Fernet / cryptography).
    # Generate with scripts/gen_secrets.py.
    ENCRYPTION_KEY: str = _get("ENCRYPTION_KEY", required=True)

    ADMIN_USERNAME: str = _get("ADMIN_USERNAME", "admin")
    # bcrypt hash, never a plaintext password. Generate with scripts/gen_secrets.py.
    ADMIN_PASSWORD_HASH: str = _get("ADMIN_PASSWORD_HASH", required=True)

    SESSION_TTL_SECONDS: int = int(_get("SESSION_TTL_SECONDS", "3600"))

    # Only set this true when actually serving over HTTPS (prod). Cookies
    # with Secure=True are silently dropped by browsers over plain HTTP,
    # so local http-only dev should set COOKIE_SECURE=false.
    COOKIE_SECURE: bool = _get("COOKIE_SECURE", "true").lower() == "true"

    # Basic anti-abuse limits (per IP).
    RATE_LIMIT_CHAT: str = _get("RATE_LIMIT_CHAT", "20/minute")
    RATE_LIMIT_LOGIN: str = _get("RATE_LIMIT_LOGIN", "5/minute")

    MAX_UPLOAD_IMAGE_BYTES: int = 4 * 1024 * 1024
    MAX_UPLOAD_DOC_BYTES: int = 8 * 1024 * 1024
    KB_MAX_CHARS_PER_DOC: int = 50_000
    KB_MAX_TOTAL_ENTRIES: int = 100

    # ── Chat session cap ──────────────────────────────────────────
    MAX_CHAT_EXCHANGES: int = int(_get("MAX_CHAT_EXCHANGES", "15"))

    # ── Appointment booking ────────────────────────────────────────
    APPT_TIMEZONE: str = _validate_timezone(_get("APPT_TIMEZONE", "Asia/Kuwait"))
    APPT_SLOT_MINUTES: int = int(_get("APPT_SLOT_MINUTES", "30"))
    APPT_DAY_START_HOUR: int = int(_get("APPT_DAY_START_HOUR", "9"))
    APPT_DAY_END_HOUR: int = int(_get("APPT_DAY_END_HOUR", "17"))
    APPT_WORKDAYS: list[int] = [
        int(d) for d in _get("APPT_WORKDAYS", "0,1,2,3,4").split(",") if d.strip() != ""
    ]  # Mon=0 .. Sun=6
    APPT_MAX_DAYS_AHEAD: int = int(_get("APPT_MAX_DAYS_AHEAD", "30"))
    RATE_LIMIT_APPOINTMENT: str = _get("RATE_LIMIT_APPOINTMENT", "6/hour")

    # Google Calendar (optional — booking still works locally if unset,
    # it just won't sync to a calendar). Service-account file must be
    # shared ("make changes to events") with GOOGLE_CALENDAR_ID.
    GOOGLE_SERVICE_ACCOUNT_FILE: str = _get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    GOOGLE_CALENDAR_ID: str = _get("GOOGLE_CALENDAR_ID", "")


settings = Settings()
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
(settings.PUBLIC_DIR / "static" / "images").mkdir(parents=True, exist_ok=True)
