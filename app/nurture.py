"""
The nurture step: a lead who shared their contact details but never
booked gets exactly one automatic follow-up email a few hours later.

Deliberately one message, one time — not a drip sequence or a rules
builder. That's the whole point: it plugs the "chat happened, then
nothing" leak without adding anything for a business owner to configure.

Runs as a lightweight background thread started at app startup (see
app/main.py). No task queue or cron needed — it just wakes up on an
interval, sends whatever's due, and goes back to sleep.
"""
import datetime
import logging
import threading

from app.config import settings
from app import storage, email_util

log = logging.getLogger("perennia.nurture")

_stop = threading.Event()


def _due_leads() -> list[dict]:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=settings.NURTURE_DELAY_HOURS)
    due = []
    for lead in storage.load_leads():
        if lead.get("status") != "new":
            continue  # already contacted, booked, or marked lost — nothing to nudge
        if lead.get("nurture_sent_at"):
            continue  # the one follow-up already went out
        try:
            created = datetime.datetime.fromisoformat(lead["created_at"])
        except (KeyError, ValueError, TypeError):
            continue
        if created <= cutoff:
            due.append(lead)
    return due


def _nurture_copy(lead: dict) -> tuple[str, str]:
    name = lead.get("name", "")
    if lead.get("lang") == "ar":
        subject = "هل ما زلت مهتماً؟ — بيرينيا"
        body = (
            f"مرحباً {name}،\n\n"
            "لاحظنا أنك تواصلت معنا مؤخراً ولم تكمل حجز موعد بعد.\n\n"
            "إذا كنت لا تزال مهتماً، يسعدنا مساعدتك — يمكنك حجز موعد مباشرة "
            "من خلال نافذة الدردشة على موقعنا في أي وقت يناسبك.\n\n— بيرينيا"
        )
    else:
        subject = "Still thinking it over?"
        body = (
            f"Hi {name},\n\n"
            "We noticed you reached out recently but didn't get a chance to "
            "book a time with us.\n\n"
            "If you're still interested, we'd love to help — you can grab a "
            "slot any time straight from the chat widget on our site.\n\n— Perennia"
        )
    return subject, body


def run_once() -> int:
    """Send the nurture email to every lead currently due one.

    Returns the number actually sent. If SMTP isn't configured at all,
    this deliberately does nothing and touches no lead records — so
    once an admin sets SMTP_* later, the backlog sends on the next tick
    instead of having been silently marked "attempted" while nothing
    was ever configured to send it.
    """
    if not settings.NURTURE_ENABLED or not email_util.is_configured():
        return 0

    due = _due_leads()
    if not due:
        return 0

    leads = storage.load_leads()
    by_id = {l["id"]: l for l in leads}
    sent = 0
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    for lead in due:
        target = by_id.get(lead["id"])
        if target is None:
            continue
        subject, body = _nurture_copy(lead)
        ok = False
        try:
            ok = email_util.send_email(lead.get("email", ""), subject, body)
        except Exception as e:
            log.warning("Nurture email failed for lead %s: %s", lead.get("id"), e)

        # Mark attempted either way, once SMTP is genuinely configured:
        # a single stale/bouncing address shouldn't retry forever — one
        # attempt is the whole design, not a retry queue.
        target["nurture_sent_at"] = now_iso
        target["status"] = "contacted"
        existing_notes = target.get("notes") or ""
        auto_note = f"[Auto] Follow-up email {'sent' if ok else 'attempted'} {now_iso}"
        target["notes"] = f"{existing_notes}\n{auto_note}" if existing_notes else auto_note
        if ok:
            sent += 1

    storage.save_leads(leads)
    return sent


def _loop() -> None:
    interval_seconds = max(1, settings.NURTURE_CHECK_INTERVAL_MINUTES) * 60
    while not _stop.is_set():
        try:
            n = run_once()
            if n:
                log.info("Nurture: sent %d follow-up email(s)", n)
        except Exception as e:
            log.warning("Nurture loop iteration failed: %s", e)
        _stop.wait(interval_seconds)


def start() -> None:
    if not settings.NURTURE_ENABLED:
        log.info("Nurture step disabled (NURTURE_ENABLED=false)")
        return
    threading.Thread(target=_loop, daemon=True, name="nurture-loop").start()
    log.info(
        "Nurture loop started (delay=%dh, interval=%dm)",
        settings.NURTURE_DELAY_HOURS, settings.NURTURE_CHECK_INTERVAL_MINUTES,
    )
