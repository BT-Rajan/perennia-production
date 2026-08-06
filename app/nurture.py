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
from app import storage, email_util, whatsapp

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


_DEFAULT_COPY = {
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
}


def _nurture_copy(lead: dict) -> tuple[str, str]:
    """Admin-editable via the Leads panel -> config.json["nurture"]. Falls
    back to _DEFAULT_COPY for any key an admin hasn't customized (or on an
    older data.json predating this setting) so a partial/missing config
    never breaks the send. {name} is substituted here, server-side --
    distinct from the {brand} placeholder used elsewhere in the app, which
    is only ever substituted in the browser and never reaches an email."""
    cfg = storage.load_config().get("nurture", {})
    is_ar = lead.get("lang") == "ar"
    suffix = "ar" if is_ar else "en"
    name = lead.get("name", "")
    subject = cfg.get(f"subject-{suffix}") or _DEFAULT_COPY[f"subject-{suffix}"]
    body = cfg.get(f"body-{suffix}") or _DEFAULT_COPY[f"body-{suffix}"]
    return subject.replace("{name}", name), body.replace("{name}", name)


def run_once() -> int:
    """Send the nurture follow-up (email and/or WhatsApp) to every lead
    currently due one.

    Returns the number of leads that got at least one channel delivered.
    If neither channel is configured at all, this deliberately does
    nothing and touches no lead records — so once an admin sets one up
    later, the backlog sends on the next tick instead of having been
    silently marked "attempted" while nothing was ever configured to
    send it.
    """
    email_ready = email_util.is_configured()
    wa_ready = whatsapp.is_configured() and bool(settings.WHATSAPP_TEMPLATE_NURTURE)
    if not settings.NURTURE_ENABLED or not (email_ready or wa_ready):
        return 0

    due = _due_leads()
    if not due:
        return 0

    # Phase 1 — the actual sends. Deliberately outside any lock: these are
    # slow network calls (SMTP round-trip, WhatsApp Cloud API), and holding
    # BOOKING_LOCK for their duration would stall live booking/reschedule/
    # cancel requests and admin lead edits for however long a send backlog
    # takes. Nothing in this phase touches storage.
    results: dict[str, tuple[bool, bool]] = {}
    for lead in due:
        ok_email = False
        if email_ready:
            subject, body = _nurture_copy(lead)
            try:
                ok_email = email_util.send_email(lead.get("email", ""), subject, body)
            except Exception as e:
                log.warning("Nurture email failed for lead %s: %s", lead.get("id"), e)

        ok_wa = False
        if wa_ready and lead.get("phone"):
            lang_code = settings.WHATSAPP_TEMPLATE_LANG_AR if lead.get("lang") == "ar" else settings.WHATSAPP_TEMPLATE_LANG_EN
            try:
                ok_wa = whatsapp.send_template(
                    lead["phone"], settings.WHATSAPP_TEMPLATE_NURTURE, lang_code, [lead.get("name", "")],
                )
            except Exception as e:
                log.warning("Nurture WhatsApp failed for lead %s: %s", lead.get("id"), e)

        results[lead["id"]] = (ok_email, ok_wa)

    # Phase 2 — apply the outcome. Short, in-memory, no network calls, so
    # this is the only part that needs the lock. It re-reads fresh data
    # rather than reusing the Phase 1 snapshot, and re-checks each lead is
    # still "new" before touching it — so a booking or admin edit that
    # landed while a slow send was in flight doesn't get clobbered by a
    # stale "contacted" write.
    sent = 0
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with storage.BOOKING_LOCK:
        leads = storage.load_leads()
        by_id = {l["id"]: l for l in leads}
        for lead_id, (ok_email, ok_wa) in results.items():
            target = by_id.get(lead_id)
            if target is None or target.get("status") != "new":
                continue
            target["nurture_sent_at"] = now_iso
            target["status"] = "contacted"
            channels = [c for c, ok in (("email", ok_email), ("WhatsApp", ok_wa)) if ok]
            outcome = "sent via " + " + ".join(channels) if channels else "attempted, no channel delivered"
            existing_notes = target.get("notes") or ""
            auto_note = f"[Auto] Follow-up {outcome} {now_iso}"
            target["notes"] = f"{existing_notes}\n{auto_note}" if existing_notes else auto_note
            if channels:
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
