"""
Persistence for config.json and knowledge_base.json.

Both files live in settings.DATA_DIR, which is never mounted as a static
directory by main.py — there is no URL that serves them directly. All
reads/writes go through this module so every access point gets the same
atomic-write and schema-default handling.
"""
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from app.config import settings
from app.security import decrypt_secret, encrypt_secret

_lock = threading.Lock()

# Serializes the full "check availability, then write" sequence for
# appointment booking. Guarding writes alone isn't enough here, since the
# race is between the availability check and the later insert, not inside
# add_appointment() itself.
BOOKING_LOCK = threading.Lock()

CONFIG_PATH = settings.DATA_DIR / "config.json"
KB_PATH = settings.DATA_DIR / "knowledge_base.json"
APPT_PATH = settings.DATA_DIR / "appointments.json"
STATS_PATH = settings.DATA_DIR / "daily_stats.json"
STATS_RETENTION_DAYS = 90

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "baseUrl": "",
    "apiKeyEncrypted": "",   # Fernet-encrypted at rest; never plaintext on disk
    "tone": "",
    "knowledge": {},         # ck-about-en, ck-vision-en, ... (see frontend)
    "contact": {
        "ct-email": "info@perennia.com",
        "ct-phone": "+965 0000 0000",
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
        # ── Brand identity (drives <title>, wordmark fallback, footer,
        #    chat header/greeting) — every visible brand string is admin-
        #    editable here instead of hardcoded in the page. ──
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
}


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp_path = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex}")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)  # atomic on POSIX


def load_config() -> dict[str, Any]:
    with _lock:
        if not CONFIG_PATH.exists():
            return dict(DEFAULT_CONFIG)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return dict(DEFAULT_CONFIG)
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged


def save_config(config: dict[str, Any]) -> None:
    with _lock:
        _atomic_write_json(CONFIG_PATH, config)


def get_decrypted_api_key(config: dict[str, Any] | None = None) -> str:
    config = config or load_config()
    return decrypt_secret(config.get("apiKeyEncrypted", ""))


def set_api_key(config: dict[str, Any], plaintext_key: str) -> dict[str, Any]:
    config["apiKeyEncrypted"] = encrypt_secret(plaintext_key)
    return config


def load_knowledge_base() -> list[dict[str, Any]]:
    with _lock:
        if not KB_PATH.exists():
            return []
        try:
            with open(KB_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []


def save_knowledge_base(entries: list[dict[str, Any]]) -> None:
    with _lock:
        _atomic_write_json(KB_PATH, entries)


# ═══════════════════════════════════════════════════════════════════
# Appointments
# ═══════════════════════════════════════════════════════════════════

def load_appointments() -> list[dict[str, Any]]:
    with _lock:
        if not APPT_PATH.exists():
            return []
        try:
            with open(APPT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []


def save_appointments(entries: list[dict[str, Any]]) -> None:
    with _lock:
        _atomic_write_json(APPT_PATH, entries)


def add_appointment(entry: dict[str, Any]) -> dict[str, Any]:
    entries = load_appointments()
    entries.append(entry)
    save_appointments(entries)
    return entry


# ═══════════════════════════════════════════════════════════════════
# Daily interaction stats (admin analytics — no PII, just counters)
# ═══════════════════════════════════════════════════════════════════

def _load_stats() -> dict[str, Any]:
    with _lock:
        if not STATS_PATH.exists():
            return {}
        try:
            with open(STATS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}


def _save_stats(data: dict[str, Any]) -> None:
    # Prune old days so this file can never grow unbounded.
    if len(data) > STATS_RETENTION_DAYS:
        for day in sorted(data.keys())[: len(data) - STATS_RETENTION_DAYS]:
            data.pop(day, None)
    with _lock:
        _atomic_write_json(STATS_PATH, data)


def record_interaction(date_str: str, session_id: str) -> None:
    data = _load_stats()
    day = data.setdefault(date_str, {"messages": 0, "sessions": []})
    day["messages"] += 1
    if session_id and session_id not in day["sessions"]:
        day["sessions"].append(session_id)
    _save_stats(data)


def record_appointment_stat(date_str: str) -> None:
    data = _load_stats()
    day = data.setdefault(date_str, {"messages": 0, "sessions": [], "appointments": 0})
    day["appointments"] = day.get("appointments", 0) + 1
    _save_stats(data)


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
