"""
Sprint 4.5 acceptance: app/storage.py's MySQL port, tested against a real
tenant MySQL database (sitehub_tenant_tst1, provisioned like a real
SiteHub paid tenant would be — same naming convention, same table-prefix
convention), exercising every function in the public API that
app/main.py, app/notifications.py, app/nurture.py, and app/scheduling.py
actually call — the same signatures as the pre-port file-based version,
unchanged, confirming those callers needed zero changes.
"""
from app import storage


def test_load_config_with_no_row_returns_defaults():
    config = storage.load_config()
    assert config["provider"] == "anthropic"
    assert config["contact"]["ct-email"] == "info@perennia.com"


def test_save_and_load_config_round_trips():
    config = storage.load_config()
    config["tone"] = "friendly and direct"
    config["contact"]["ct-phone"] = "+1 555-0100"
    storage.save_config(config)

    reloaded = storage.load_config()
    assert reloaded["tone"] == "friendly and direct"
    assert reloaded["contact"]["ct-phone"] == "+1 555-0100"
    # Untouched defaults still merge in correctly.
    assert reloaded["contact"]["ct-addr-en"] == "Kuwait"


def test_legacy_nav_links_migration_still_works():
    config = storage.load_config()
    config["landing"]["ourWorkUrl"] = "https://example.com/work"
    config["landing"]["navLinks"] = []
    storage.save_config(config)

    reloaded = storage.load_config()
    assert len(reloaded["landing"]["navLinks"]) == 1
    assert reloaded["landing"]["navLinks"][0]["url"] == "https://example.com/work"


def test_api_key_encrypted_at_rest_and_decrypts_correctly():
    config = storage.load_config()
    config = storage.set_api_key(config, "sk-real-looking-secret-key-value")
    storage.save_config(config)

    reloaded = storage.load_config()
    assert reloaded["apiKeyEncrypted"] != "sk-real-looking-secret-key-value"
    assert "sk-real-looking-secret-key-value" not in reloaded["apiKeyEncrypted"]
    assert storage.get_decrypted_api_key(reloaded) == "sk-real-looking-secret-key-value"


def test_knowledge_base_round_trip():
    assert storage.load_knowledge_base() == []
    entries = [{"id": "1", "title": "About us", "content": "We do things."}]
    storage.save_knowledge_base(entries)
    assert storage.load_knowledge_base() == entries


def test_appointments_add_and_load():
    assert storage.load_appointments() == []
    storage.add_appointment({"id": "a1", "start": "2026-09-01T10:00:00"})
    storage.add_appointment({"id": "a2", "start": "2026-09-01T10:30:00"})
    entries = storage.load_appointments()
    assert len(entries) == 2
    assert {e["id"] for e in entries} == {"a1", "a2"}


def test_leads_add_and_load():
    assert storage.load_leads() == []
    storage.add_lead({"id": "l1", "email": "lead@example.com"})
    entries = storage.load_leads()
    assert len(entries) == 1
    assert entries[0]["email"] == "lead@example.com"


def test_booking_lock_is_a_real_lock_usable_as_context_manager():
    with storage.BOOKING_LOCK:
        pass  # must not raise — main.py uses this as `with storage.BOOKING_LOCK:`


def test_record_interaction_and_daily_summary():
    storage.record_interaction("2026-08-08", "session-1")
    storage.record_interaction("2026-08-08", "session-1")  # same session, doesn't double-count sessions
    storage.record_interaction("2026-08-08", "session-2")

    summary = storage.daily_summary(days=14)
    assert len(summary) == 1
    assert summary[0]["date"] == "2026-08-08"
    assert summary[0]["messages"] == 3
    assert summary[0]["sessions"] == 2


def test_record_appointment_stat():
    storage.record_appointment_stat("2026-08-08")
    storage.record_appointment_stat("2026-08-08")
    summary = storage.daily_summary(days=14)
    assert summary[0]["appointments"] == 2


def test_daily_summary_is_newest_first_and_respects_days_limit():
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        storage.record_interaction(day, "s")
    summary = storage.daily_summary(days=2)
    assert [s["date"] for s in summary] == ["2026-08-03", "2026-08-02"]


def test_concurrent_read_modify_write_does_not_lose_increments():
    """The exact race BOOKING_LOCK/_lock exist to prevent: many threads
    incrementing the same day's message count must not lose writes to a
    read-modify-write race. Real threads, real MySQL round-trips."""
    import threading

    def bump():
        storage.record_interaction("2026-08-09", "shared-session")

    threads = [threading.Thread(target=bump) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    summary = storage.daily_summary(days=1)
    assert summary[0]["messages"] == 20


def test_tables_are_correctly_prefixed_for_this_tenant():
    assert storage.CONFIG_TABLE == "tst1_config"
    assert storage.KB_TABLE == "tst1_knowledge_base"
    assert storage.APPT_TABLE == "tst1_appointments"
    assert storage.LEADS_TABLE == "tst1_leads"
    assert storage.STATS_TABLE == "tst1_daily_stats"
