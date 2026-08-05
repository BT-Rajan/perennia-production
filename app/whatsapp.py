"""
Outbound WhatsApp messages via Meta's WhatsApp Cloud API.

Mirrors app/email_util.py's pattern: if it isn't configured, every send
function is a silent no-op (logged at warning level) — nothing else in
the app breaks or blocks on it.

Two distinct capabilities, because WhatsApp Business policy treats them
differently:

  - send_template()  Business-initiated message to a number that hasn't
                      messaged in — a booking confirmation, a nurture
                      nudge. Requires an approved message template (create
                      + get these approved in Meta Business Manager first;
                      this module only calls a template by name, it can't
                      create one). Without WHATSAPP_TEMPLATE_* configured,
                      the specific send it's used for is skipped.

  - send_text()       Free-form text. Only deliverable within the 24-hour
                      customer-service window after the visitor messages
                      the business number first. Useful for a reply loop,
                      not for the first outbound contact.

Both are best-effort: any failure is logged and swallowed, never raised
to the caller, same as email_util.send_email.
"""
import logging

import httpx

from app.config import settings

log = logging.getLogger("perennia.whatsapp")

_GRAPH_TIMEOUT = 10.0


def is_configured() -> bool:
    return bool(
        settings.WHATSAPP_ENABLED
        and settings.WHATSAPP_ACCESS_TOKEN
        and settings.WHATSAPP_PHONE_NUMBER_ID
    )


def _post(payload: dict) -> bool:
    url = (
        f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/"
        f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=_GRAPH_TIMEOUT)
        if resp.status_code >= 400:
            log.warning("WhatsApp send failed (%d): %s", resp.status_code, resp.text[:500])
            return False
        return True
    except Exception as e:
        log.warning("WhatsApp send raised: %s", e)
        return False


def _normalize(to: str) -> str:
    # Cloud API wants digits only (country code + number, no '+', spaces,
    # or punctuation).
    return "".join(ch for ch in to if ch.isdigit())


def send_text(to: str, body: str) -> bool:
    """Free-form text — only deliverable inside the 24h session window."""
    if not to or not is_configured():
        return False
    payload = {
        "messaging_product": "whatsapp",
        "to": _normalize(to),
        "type": "text",
        "text": {"body": body},
    }
    return _post(payload)


def send_template(to: str, template_name: str, lang_code: str, body_params: list[str] | None = None) -> bool:
    """Approved-template send — the only reliable way to message someone
    who hasn't messaged the business number first. `template_name` and
    `lang_code` must match a template already approved in Meta Business
    Manager exactly, including its variable count/order."""
    if not to or not template_name or not is_configured():
        return False
    components = []
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": p} for p in body_params],
        })
    payload = {
        "messaging_product": "whatsapp",
        "to": _normalize(to),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang_code},
            "components": components,
        },
    }
    return _post(payload)
