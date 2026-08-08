"""
Persistence for config, knowledge_base, appointments, leads, and daily
stats — MySQL-backed (this instance's own dedicated tenant database,
provisioned by SiteHub's paid-tier pipeline before this app ever starts).

Port note: every table this app creates is named
f"{settings.DB_TABLE_PREFIX}_{name}" (e.g. "k3f9_config") — matching
SiteHub's DB-per-tenant table-prefix convention, so this tenant's
database can be exported/attached/merged elsewhere without table-name
collisions with anything else.

This is a deliberately narrow, faithful port: each of the five original
JSON files (config.json, knowledge_base.json, appointments.json,
leads.json, daily_stats.json) becomes exactly one single-row table
holding that same JSON document, rather than a normalized relational
redesign. That's not a shortcut — every call site in app/main.py,
app/notifications.py, app/nurture.py, and app/scheduling.py always
loads/saves the WHOLE list or dict at once (filtering/sorting happens in
Python after loading), so a relational redesign would buy nothing this
sprint and would be a much bigger, riskier change than "replace the
storage layer" calls for. The public function signatures below are
UNCHANGED from the pre-port file-based storage.py — every caller across
the app needed zero changes.

Concurrency: the in-process threading.Lock()/BOOKING_LOCK from the
file-based version are kept as-is. This app still runs as a single
process (see main.py's note on the now-removed instance lock) — MySQL
being the backing store doesn't by itself change that, and the same
in-process locks that correctly serialized file writes correctly
serialize these now-MySQL reads/writes too, without introducing a new
DB-level locking scheme this sprint didn't ask for.
"""
import json
import threading
from typing import Any

from sqlalchemy import create_engine, text

from app.config import settings
from app.security import decrypt_secret, encrypt_secret

_lock = threading.Lock()

# Serializes the full "check availability, then write" sequence for
# appointment booking. Guarding writes alone isn't enough here, since the
# race is between the availability check and the later insert, not inside
# add_appointment() itself.
BOOKING_LOCK = threading.Lock()

STATS_RETENTION_DAYS = 90

_PREFIX = settings.DB_TABLE_PREFIX
CONFIG_TABLE = f"{_PREFIX}_config"
KB_TABLE = f"{_PREFIX}_knowledge_base"
APPT_TABLE = f"{_PREFIX}_appointments"
LEADS_TABLE = f"{_PREFIX}_leads"
STATS_TABLE = f"{_PREFIX}_daily_stats"

# One small, dedicated engine for this instance's own tenant database —
# unlike SiteHub's shared free-tier engine, this process serves exactly
# one tenant for its whole lifetime, so there's no per-tenant engine
# cache to build here; a single connection pool is the right shape.
_engine = create_engine(settings.DATABASE_URL, pool_size=2, max_overflow=2, pool_recycle=280, pool_pre_ping=True)

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "baseUrl": "",
    "apiKeyEncrypted": "",   # Fernet-encrypted at rest; never plaintext in the DB
    "tone": "",
    "knowledge": {},         # ck-about-en, ck-vision-en, ... (see frontend)
    "contact": {
        "ct-email": "info@perennia.com",
        "ct-phone": "+965 0000 0000",
        "ct-whatsapp": "",        # public click-to-chat number; blank = button hidden
        "ct-addr-en": "Kuwait",
        "ct-addr-ar": "الكويت",
    },
    "landing": {
        "welcomeText-en": "Welcome to Perennia",
        "welcomeText-ar": "مرحبا بك في بيرينيا",
        "tagline-en": "Visit our V-Lounge for more",
        "tagline-ar": "زر V-Lounge الخاص بنا لمزيد من المعلومات",
        "ourWorkUrl": "",
        "contactUrl": "",
        "navLinks": [],
        "brandName-en": "Perennia",
        "brandName-ar": "بيرينيا",
        "siteTitle": "PERENNIA | بيرينيا",
        "metaDescription": "Perennia — AI-powered technology and innovation.",
        "footerText-en": "© 2026 PERENNIA",
        "footerText-ar": "© 2026 بيرينيا",
        "chatHeader-en": "'s AI Assistant — here to help",
        "chatHeader-ar": " الذكي — هنا لمساعدتك",
        "chatGreeting-en": "Hello! I'm {brand}'s AI assistant. Ask me anything about our company — our solutions, products, industries we serve, or what makes us different.",
        "chatGreeting-ar": "مرحباً! أنا المساعد الذكي لـ{brand}. اسألني أي شيء عن شركتنا — حلولنا، منتجاتنا، الصناعات التي نخدمها، أو ما يميزنا.",
        "chatChips-en": [
            "What does {brand} do?",
            "Tell me about your products",
            "Which industries do you serve?",
            "What makes you different?",
        ],
        "chatChips-ar": [
            "ماذا تفعل {brand}؟",
            "أخبرني عن منتجاتكم",
            "ما هي الصناعات التي تخدمونها؟",
            "ما الذي يميزكم؟",
        ],
        "sidebarImageAlt-en": "Perennia Solution",
        "sidebarImageAlt-ar": "حل بيرينيا",
    },
    "booking": {
        "promptsEn": [
            "Would you like me to schedule a call with our Growth Strategist?",
            "Shall I book an appointment with our Growth Strategist for you?",
            "Interested in speaking with our Growth Strategist? I can set that up.",
            "Want to chat with our Growth Strategist? I can book a time.",
            "Would a call with our Growth Strategist be helpful?",
        ],
        "promptsAr": [
            "هل تود لي أن أحجز لك موعداً مع خبيرنا في النمو؟",
            "هل تود لي أن أحجز لك مكالمة مع خبيرنا في النمو؟",
            "هل تود التحدث مع خبيرنا في النمو؟ يمكنني ترتيب ذلك.",
            "هل مكالمة مع خبيرنا في النمو مفيدة لك؟",
            "هل ترغب في جدولة موعد مع خبيرنا في النمو؟",
        ],
        "enabled": True,
    },
    "nurture": {
        "subject-en": "Still thinking it over?",
        "body-en": (
            "Hi {name},\n\n"
            "We noticed you reached out recently but didn't get a chance to "
            "book a time with us.\n\n"
            "If you're still interested, we'd love to help — you can grab a "
            "slot any time straight from the chat widget on our site.\n\n— Perennia"
        ),
        "subject-ar": "هل ما زلت مهتماً؟",
        "body-ar": (
            "مرحباً {name}،\n\n"
            "لاحظنا أنك تواصلت معنا مؤخراً ولم تكمل حجز موعد بعد.\n\n"
            "إذا كنت لا تزال مهتماً، يسعدنا مساعدتك — يمكنك حجز موعد مباشرة "
            "من خلال نافذة الدردشة على موقعنا في أي وقت يناسبك.\n\n— بيرينيا"
        ),
    },
}


def ensure_tables() -> None:
    """Creates this instance's tables if they don't exist yet — called
    once at import time below. Idempotent (CREATE TABLE IF NOT EXISTS),
    safe to call on every startup rather than needing a separate
    migration-runner step for what's still, deliberately, a very simple
    schema (single JSON-blob-per-table, per this module's docstring)."""
    with _engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS `{CONFIG_TABLE}` ("
            f"id TINYINT PRIMARY KEY, config JSON NOT NULL, updated_at DATETIME NOT NULL)"
        ))
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS `{KB_TABLE}` ("
            f"id TINYINT PRIMARY KEY, entries JSON NOT NULL, updated_at DATETIME NOT NULL)"
        ))
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS `{APPT_TABLE}` ("
            f"id TINYINT PRIMARY KEY, entries JSON NOT NULL, updated_at DATETIME NOT NULL)"
        ))
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS `{LEADS_TABLE}` ("
            f"id TINYINT PRIMARY KEY, entries JSON NOT NULL, updated_at DATETIME NOT NULL)"
        ))
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS `{STATS_TABLE}` ("
            f"id TINYINT PRIMARY KEY, data JSON NOT NULL, updated_at DATETIME NOT NULL)"
        ))


def _load_blob(table: str, column: str, default):
    with _engine.connect() as conn:
        row = conn.execute(text(f"SELECT `{column}` FROM `{table}` WHERE id = 1")).fetchone()
    if not row or row[0] is None:
        return default
    value = row[0]
    # pymysql returns a MySQL JSON column as raw text, not auto-parsed
    # into a Python object (unlike, e.g., psycopg2's JSONB handling) — a
    # plain `text()` query bypasses SQLAlchemy's JSON type machinery
    # entirely, so this has to decode explicitly.
    return value if isinstance(value, (dict, list)) else json.loads(value)


def _save_blob(table: str, column: str, value) -> None:
    with _engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(
            text(
                f"INSERT INTO `{table}` (id, `{column}`, updated_at) VALUES (1, :value, NOW()) "
                f"ON DUPLICATE KEY UPDATE `{column}` = VALUES(`{column}`), updated_at = NOW()"
            ),
            {"value": json.dumps(value)},
        )


def _migrate_legacy_nav_links(config: dict[str, Any]) -> None:
    """Upgrade path for configs written before the generic nav-links list
    existed: they only had two fixed fields (ourWorkUrl/contactUrl). Fold
    those into navLinks (only if that list is still empty, so this never
    clobbers links an admin has already configured through the new UI).
    Always reassigns a fresh dict to config["landing"] rather than mutating
    in place, since that dict may still be the DEFAULT_CONFIG one."""
    landing = dict(config.get("landing") or {})
    if not landing.get("navLinks"):
        legacy = []
        if landing.get("ourWorkUrl"):
            legacy.append({
                "id": "legacy-our-work", "label_en": "Our Work", "label_ar": "أعمالنا",
                "url": landing["ourWorkUrl"], "content_en": "", "content_ar": "",
            })
        if landing.get("contactUrl"):
            legacy.append({
                "id": "legacy-contact", "label_en": "Contact Us", "label_ar": "اتصل بنا",
                "url": landing["contactUrl"], "content_en": "", "content_ar": "",
            })
        if legacy:
            landing["navLinks"] = legacy
    config["landing"] = landing


def load_config() -> dict[str, Any]:
    with _lock:
        data = _load_blob(CONFIG_TABLE, "config", None)
        if data is None:
            return dict(DEFAULT_CONFIG)
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        _migrate_legacy_nav_links(merged)
        return merged


def save_config(config: dict[str, Any]) -> None:
    with _lock:
        _save_blob(CONFIG_TABLE, "config", config)


def get_decrypted_api_key(config: dict[str, Any] | None = None) -> str:
    config = config or load_config()
    return decrypt_secret(config.get("apiKeyEncrypted", ""))


def set_api_key(config: dict[str, Any], plaintext_key: str) -> dict[str, Any]:
    config["apiKeyEncrypted"] = encrypt_secret(plaintext_key)
    return config


def load_knowledge_base() -> list[dict[str, Any]]:
    with _lock:
        data = _load_blob(KB_TABLE, "entries", None)
        return data if isinstance(data, list) else []


def save_knowledge_base(entries: list[dict[str, Any]]) -> None:
    with _lock:
        _save_blob(KB_TABLE, "entries", entries)


# ═══════════════════════════════════════════════════════════════════
# Appointments
# ═══════════════════════════════════════════════════════════════════

def load_appointments() -> list[dict[str, Any]]:
    with _lock:
        data = _load_blob(APPT_TABLE, "entries", None)
        return data if isinstance(data, list) else []


def save_appointments(entries: list[dict[str, Any]]) -> None:
    with _lock:
        _save_blob(APPT_TABLE, "entries", entries)


def add_appointment(entry: dict[str, Any]) -> dict[str, Any]:
    entries = load_appointments()
    entries.append(entry)
    save_appointments(entries)
    return entry


# ═══════════════════════════════════════════════════════════════════
# Leads (contact details captured before a visitor starts chatting)
# ═══════════════════════════════════════════════════════════════════

def load_leads() -> list[dict[str, Any]]:
    with _lock:
        data = _load_blob(LEADS_TABLE, "entries", None)
        return data if isinstance(data, list) else []


def save_leads(entries: list[dict[str, Any]]) -> None:
    with _lock:
        _save_blob(LEADS_TABLE, "entries", entries)


def add_lead(entry: dict[str, Any]) -> dict[str, Any]:
    entries = load_leads()
    entries.append(entry)
    save_leads(entries)
    return entry


# ═══════════════════════════════════════════════════════════════════
# Daily interaction stats (admin analytics — no PII, just counters)
# ═══════════════════════════════════════════════════════════════════

def _read_stats_unlocked() -> dict[str, Any]:
    """Caller must hold _lock. Not exported — see _load_stats() / the
    record_* functions below for the locked entry points."""
    data = _load_blob(STATS_TABLE, "data", None)
    return data if isinstance(data, dict) else {}


def _write_stats_unlocked(data: dict[str, Any]) -> None:
    """Caller must hold _lock."""
    # Prune old days so this row can never grow unbounded.
    if len(data) > STATS_RETENTION_DAYS:
        for day in sorted(data.keys())[: len(data) - STATS_RETENTION_DAYS]:
            data.pop(day, None)
    _save_blob(STATS_TABLE, "data", data)


def _load_stats() -> dict[str, Any]:
    with _lock:
        return _read_stats_unlocked()


def record_interaction(date_str: str, session_id: str) -> None:
    # The whole read-modify-write must be one critical section, or two
    # concurrent chat requests can both read the same counts and each
    # write back N+1, silently losing one increment.
    with _lock:
        data = _read_stats_unlocked()
        day = data.setdefault(date_str, {"messages": 0, "sessions": []})
        day["messages"] += 1
        if session_id and session_id not in day["sessions"]:
            day["sessions"].append(session_id)
        _write_stats_unlocked(data)


def record_appointment_stat(date_str: str) -> None:
    with _lock:
        data = _read_stats_unlocked()
        day = data.setdefault(date_str, {"messages": 0, "sessions": [], "appointments": 0})
        day["appointments"] = day.get("appointments", 0) + 1
        _write_stats_unlocked(data)


def daily_summary(days: int = 14) -> list[dict[str, Any]]:
    """Newest-first list of {date, messages, sessions, appointments}."""
    data = _load_stats()
    dates = sorted(data.keys(), reverse=True)[:days]
    return [
        {
            "date": d,
            "messages": data[d].get("messages", 0),
            "sessions": len(data[d].get("sessions", [])),
            "appointments": data[d].get("appointments", 0),
        }
        for d in dates
    ]


ensure_tables()
