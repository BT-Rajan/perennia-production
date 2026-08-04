"""
Minimal SMTP email sending, used for appointment lifecycle notifications.

If SMTP_HOST / SMTP_FROM are not configured, send_email() is a silent
no-op (logged at warning level) — booking, rescheduling, and cancelling
appointments still work locally, they just won't trigger emails until an
admin sets SMTP_* in .env. This mirrors app/gcal.py's "optional, degrades
gracefully" pattern.
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import settings

log = logging.getLogger("perennia.email")


def is_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_FROM)


def send_email(to: str, subject: str, text_body: str, html_body: str | None = None) -> bool:
    if not to:
        return False
    if not is_configured():
        log.warning("SMTP not configured — skipping email to %s (%s)", to, subject)
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_FROM
        msg["To"] = to
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            if settings.SMTP_USE_TLS:
                server.starttls()
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.SMTP_FROM, [to], msg.as_string())
        return True
    except Exception as e:
        log.warning("Failed to send email to %s: %s", to, e)
        return False
