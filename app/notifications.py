"""
Builds and sends the appointment lifecycle emails (booked / rescheduled /
cancelled) to both the visitor and the admin inbox.

Callers fire these in a background thread (see app/main.py) so an SMTP
round-trip never adds latency to the booking/reschedule/cancel response.
"""
import datetime
import logging

from app.config import settings
from app import storage, email_util, whatsapp

log = logging.getLogger("perennia.notifications")


def _fmt(dt_iso: str) -> str:
    dt = datetime.datetime.fromisoformat(dt_iso)
    try:
        return dt.strftime("%A, %B %-d, %Y at %-I:%M %p")
    except ValueError:
        # -d/-I aren't supported on some platforms (notably Windows);
        # fall back to the zero-padded form and strip the leading zero.
        return dt.strftime("%A, %B %d, %Y at %I:%M %p").replace(" 0", " ")


def _admin_email() -> str:
    if settings.ADMIN_NOTIFY_EMAIL:
        return settings.ADMIN_NOTIFY_EMAIL
    contact = storage.load_config().get("contact") or {}
    return contact.get("ct-email", "")


def notify_booked(entry: dict) -> None:
    _send_pair(entry, kind="booked")


def notify_rescheduled(entry: dict, old_start: str) -> None:
    _send_pair(entry, kind="rescheduled", old_start=old_start)


def notify_cancelled(entry: dict) -> None:
    _send_pair(entry, kind="cancelled")


def _send_pair(entry: dict, kind: str, old_start: str | None = None) -> None:
    is_ar = entry.get("lang") == "ar"
    when = _fmt(entry["start"])
    appt_id = entry["id"]
    name = entry.get("name", "")

    if kind == "booked":
        if is_ar:
            subj_user = f"تم تأكيد موعدك في بيرينيا — {appt_id}"
            body_user = (
                f"مرحباً {name}،\n\nتم تأكيد موعدك مع بيرينيا.\n\n"
                f"رقم الموعد: {appt_id}\nالموعد: {when} ({settings.APPT_TIMEZONE})\n\n"
                f"لإعادة الجدولة أو الإلغاء، يمكنك ذلك من نافذة الدردشة في أي وقت حتى "
                f"{settings.APPT_MIN_NOTICE_HOURS} ساعات قبل الموعد — فقط قدّم رقم الموعد "
                f"والبريد الإلكتروني الذي استخدمته عند الحجز.\n\n— بيرينيا"
            )
        else:
            subj_user = f"Your Perennia appointment is confirmed — {appt_id}"
            body_user = (
                f"Hi {name},\n\nYour appointment with Perennia is confirmed.\n\n"
                f"Appointment ID: {appt_id}\nWhen: {when} ({settings.APPT_TIMEZONE})\n\n"
                f"Need to reschedule or cancel? You can do that from the chat widget any time "
                f"up until {settings.APPT_MIN_NOTICE_HOURS} hours before your appointment — just "
                f"provide this ID and the email you booked with.\n\n— Perennia"
            )
        subj_admin = f"New appointment booked — {name} ({appt_id})"
        body_admin = (
            f"New appointment booked.\n\n"
            f"ID: {appt_id}\nName: {name}\nEmail: {entry.get('email', '')}\n"
            f"Phone: {entry.get('phone') or '—'}\nService interest: {entry.get('service') or '—'}\n"
            f"Notes: {entry.get('notes') or '—'}\nWhen: {when} ({settings.APPT_TIMEZONE})\n"
        )

    elif kind == "rescheduled":
        old_when = _fmt(old_start) if old_start else "—"
        if is_ar:
            subj_user = f"تم تعديل موعدك في بيرينيا — {appt_id}"
            body_user = (
                f"مرحباً {name}،\n\nتم تعديل موعدك.\n\n"
                f"رقم الموعد: {appt_id}\nالموعد السابق: {old_when}\n"
                f"الموعد الجديد: {when} ({settings.APPT_TIMEZONE})\n\n— بيرينيا"
            )
        else:
            subj_user = f"Your Perennia appointment was rescheduled — {appt_id}"
            body_user = (
                f"Hi {name},\n\nYour appointment has been moved.\n\n"
                f"Appointment ID: {appt_id}\nPrevious time: {old_when}\n"
                f"New time: {when} ({settings.APPT_TIMEZONE})\n\n— Perennia"
            )
        subj_admin = f"Appointment rescheduled — {name} ({appt_id})"
        body_admin = (
            f"Appointment rescheduled.\n\nID: {appt_id}\nName: {name}\nEmail: {entry.get('email', '')}\n"
            f"Previous time: {old_when}\nNew time: {when} ({settings.APPT_TIMEZONE})\n"
        )

    else:  # cancelled
        if is_ar:
            subj_user = f"تم إلغاء موعدك في بيرينيا — {appt_id}"
            body_user = (
                f"مرحباً {name}،\n\nتم إلغاء موعدك ({when}) بناءً على طلبك.\n\n"
                f"رقم الموعد: {appt_id}\n\nيمكنك حجز موعد جديد في أي وقت.\n\n— بيرينيا"
            )
        else:
            subj_user = f"Your Perennia appointment was cancelled — {appt_id}"
            body_user = (
                f"Hi {name},\n\nYour appointment ({when}) has been cancelled as requested.\n\n"
                f"Appointment ID: {appt_id}\n\nFeel free to book a new time whenever it suits you.\n\n— Perennia"
            )
        subj_admin = f"Appointment cancelled — {name} ({appt_id})"
        body_admin = (
            f"Appointment cancelled.\n\nID: {appt_id}\nName: {name}\nEmail: {entry.get('email', '')}\n"
            f"When: {when} ({settings.APPT_TIMEZONE})\n"
        )

    try:
        email_util.send_email(entry.get("email", ""), subj_user, body_user)
        admin_to = _admin_email()
        if admin_to:
            email_util.send_email(admin_to, subj_admin, body_admin)
    except Exception as e:  # never let a notification failure surface to the caller
        log.warning("Appointment notification failed for %s: %s", appt_id, e)

    # WhatsApp is additive, not a replacement for email, and only wired up
    # for the "booked" confirmation for now (the highest-value one). Needs
    # WHATSAPP_TEMPLATE_BOOKED configured with an approved template whose
    # variables are (name, appointment id, when) in that order — if it
    # isn't set, or the visitor gave no phone number, this is a no-op.
    if kind == "booked" and entry.get("phone"):
        try:
            lang_code = settings.WHATSAPP_TEMPLATE_LANG_AR if is_ar else settings.WHATSAPP_TEMPLATE_LANG_EN
            whatsapp.send_template(
                entry["phone"],
                settings.WHATSAPP_TEMPLATE_BOOKED,
                lang_code,
                [name, appt_id, when],
            )
        except Exception as e:
            log.warning("WhatsApp notification failed for %s: %s", appt_id, e)
