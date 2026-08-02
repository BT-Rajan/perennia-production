"""
Minimal alerting (docs/06-development-passes.md, Pass 1: "a direct
notification... no dashboard"). A platform_alerts table + screen is deferred
(Pass 6) until notification volume actually justifies it. For now: cross a
threshold, send one webhook call.
"""
import json
import urllib.request
from typing import Any

from app.config import settings


class AlertDeliveryError(Exception):
    pass


def send_alert(alert_type: str, tenant_subdomain: str, detail: dict[str, Any]) -> bool:
    """
    Fires a webhook (e.g. Slack incoming webhook) if ALERT_WEBHOOK_URL is
    configured. Returns False (not an exception) when no webhook is
    configured, so tests/dev environments without alerting set up don't
    fail — but DOES raise AlertDeliveryError if a webhook *is* configured
    and the call fails, since a silently-dropped alert is worse than a loud
    failure during setup.
    """
    if not settings.ALERT_WEBHOOK_URL:
        return False

    payload = {
        "text": f"[Perennia] {alert_type} — {tenant_subdomain}: {json.dumps(detail)}"
    }
    req = urllib.request.Request(
        settings.ALERT_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status >= 300:
                raise AlertDeliveryError(f"Webhook returned status {resp.status}")
    except Exception as e:
        raise AlertDeliveryError(str(e)) from e
    return True


def check_payment_failure_threshold(recent_failure_count: int, tenant_subdomain: str) -> bool:
    """Called after recording a payment failure. Returns True if an alert fired."""
    if recent_failure_count >= settings.ALERT_PAYMENT_FAILURE_THRESHOLD:
        return send_alert(
            "payment_failure_spike",
            tenant_subdomain,
            {"recent_failures": recent_failure_count},
        )
    return False
