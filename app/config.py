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
    """Fail fast at startup on a bad APPT_TIMEZONE instead of raising the
    first time a visitor tries to book (or, worse, silently misbehaving)."""
    if not tz_name:
        print("FATAL: APPT_TIMEZONE is empty.", file=sys.stderr)
        sys.exit(1)
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz_name)
    except Exception as e:
        print(
            f"FATAL: Invalid APPT_TIMEZONE '{tz_name}': {e}\n"
            "Set it to a valid IANA timezone (e.g. 'America/New_York', 'Asia/Kuwait'). "
            "See https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
            file=sys.stderr,
        )
        sys.exit(1)
    return tz_name


class Settings:
    # Where config.json / knowledge_base.json live. MUST be outside of
    # any directory served as static files.
    DATA_DIR: Path = Path(_get("DATA_DIR", str(BASE_DIR / "data")))

    # Directory of static, public assets (site HTML, images). Nothing
    # secret is ever allowed to live under here.
    PUBLIC_DIR: Path = Path(_get("PUBLIC_DIR", str(BASE_DIR / "public")))

    PORT: int = int(_get("PORT", "8001"))
    HOST: str = _get("HOST", "127.0.0.1")

    # Server-side application logs (separate from console output). Kept
    # outside PUBLIC_DIR so log contents (which can include stack traces
    # from unhandled errors) are never web-accessible. Rotates so a busy
    # server doesn't grow this file unboundedly.
    LOG_DIR: Path = Path(_get("LOG_DIR", str(BASE_DIR / "logs")))
    LOG_FILE: str = _get("LOG_FILE", "perennia.log")
    LOG_MAX_BYTES: int = int(_get("LOG_MAX_BYTES", str(5 * 1024 * 1024)))  # 5 MB
    LOG_BACKUP_COUNT: int = int(_get("LOG_BACKUP_COUNT", "5"))

    # Optional direct HTTPS bind (e.g. for internet-facing deployments
    # without a reverse proxy). Both files must exist for this to be used;
    # otherwise the app falls back to plain HTTP on HOST:PORT above.
    # Binding to 443 normally requires Administrator/root privileges.
    HTTPS_PORT: int = int(_get("HTTPS_PORT", "443"))
    SSL_CERTFILE: str = _get("SSL_CERTFILE", "")
    SSL_KEYFILE: str = _get("SSL_KEYFILE", "")

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

    # Avatar (small, square, always heavily downscaled client-side).
    MAX_UPLOAD_IMAGE_BYTES: int = 4 * 1024 * 1024
    # Logo (frontend's own dropzone check allows up to this — a high-res
    # brand logo with transparency easily lands in the 4-8MB range, and
    # the backend previously enforced the 4MB avatar limit here too,
    # which meant a file the UI had already accepted would still get
    # rejected server-side).
    MAX_UPLOAD_LOGO_BYTES: int = 8 * 1024 * 1024
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

    # Minimum notice required to reschedule or cancel a booked appointment.
    APPT_MIN_NOTICE_HOURS: int = int(_get("APPT_MIN_NOTICE_HOURS", "6"))

    # Email (SMTP) — optional. If SMTP_HOST is unset, appointment emails are
    # skipped with a warning log; booking/reschedule/cancel still work
    # locally, they just won't trigger emails until these are configured.
    SMTP_HOST: str = _get("SMTP_HOST", "")
    SMTP_PORT: int = int(_get("SMTP_PORT", "587"))
    SMTP_USER: str = _get("SMTP_USER", "")
    SMTP_PASSWORD: str = _get("SMTP_PASSWORD", "")
    SMTP_USE_TLS: bool = _get("SMTP_USE_TLS", "true").lower() == "true"
    SMTP_FROM: str = _get("SMTP_FROM", "")
    # Where admin notification emails go. Falls back to the contact email
    # configured in the admin panel (config.json → contact.ct-email) if unset.
    ADMIN_NOTIFY_EMAIL: str = _get("ADMIN_NOTIFY_EMAIL", "")

    # ── Lead nurture ──────────────────────────────────────────────
    # One automatic follow-up email to a lead who shared contact details
    # but never booked — deliberately a single nudge, not a drip sequence.
    NURTURE_ENABLED: bool = _get("NURTURE_ENABLED", "true").lower() == "true"
    NURTURE_DELAY_HOURS: int = int(_get("NURTURE_DELAY_HOURS", "3"))
    NURTURE_CHECK_INTERVAL_MINUTES: int = int(_get("NURTURE_CHECK_INTERVAL_MINUTES", "15"))

    # ── WhatsApp (optional, via Meta's WhatsApp Cloud API) ─────────
    # Two different capabilities, because WhatsApp Business policy treats
    # them differently:
    #  1. Click-to-chat link (wa.me/<number>) — always available, needs
    #     nothing but the public business number below. This is what
    #     WHATSAPP_BUSINESS_NUMBER + the widget button use.
    #  2. Sending a message *to* someone who hasn't messaged the business
    #     first (a booking confirmation, a nurture nudge) requires the
    #     Cloud API and, per Meta policy, an approved message template —
    #     free-form text only works inside a 24h window after the visitor
    #     messages in. Without WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID
    #     and an approved template name, outbound sends are a no-op and
    #     the click-to-chat link still works fine on its own.
    WHATSAPP_ENABLED: bool = _get("WHATSAPP_ENABLED", "true").lower() == "true"
    WHATSAPP_PHONE_NUMBER_ID: str = _get("WHATSAPP_PHONE_NUMBER_ID", "")
    WHATSAPP_ACCESS_TOKEN: str = _get("WHATSAPP_ACCESS_TOKEN", "")
    WHATSAPP_API_VERSION: str = _get("WHATSAPP_API_VERSION", "v21.0")
    # Approved template names (create + get these approved in Meta Business
    # Manager first). Left blank = that specific outbound send is skipped.
    WHATSAPP_TEMPLATE_BOOKED: str = _get("WHATSAPP_TEMPLATE_BOOKED", "")
    WHATSAPP_TEMPLATE_NURTURE: str = _get("WHATSAPP_TEMPLATE_NURTURE", "")
    WHATSAPP_TEMPLATE_LANG_EN: str = _get("WHATSAPP_TEMPLATE_LANG_EN", "en_US")
    WHATSAPP_TEMPLATE_LANG_AR: str = _get("WHATSAPP_TEMPLATE_LANG_AR", "ar")


settings = Settings()
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
(settings.PUBLIC_DIR / "static" / "images").mkdir(parents=True, exist_ok=True)
settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
