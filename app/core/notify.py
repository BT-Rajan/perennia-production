"""
Notification interface (docs/06 Pass 2: WhatsApp/SMS reminders, human
handoff). Kept as a small interface with a swappable implementation rather
than hardcoding a specific provider — a real WhatsApp Business API or SMS
gateway integration needs live credentials this environment doesn't have;
the interface is what Pass 2's booking engine depends on, so a real provider
can be dropped in later without touching booking.py.
"""
import logging
from abc import ABC, abstractmethod

log = logging.getLogger("perennia.notify")


class Notifier(ABC):
    @abstractmethod
    def send_reminder(self, *, phone: str, message: str) -> bool:
        ...

    @abstractmethod
    def send_handoff_alert(self, *, tenant_subdomain: str, customer_phone: str, context: str) -> bool:
        ...


class LoggingNotifier(Notifier):
    """Dev/test implementation — logs instead of sending. Same interface a
    real WhatsApp/SMS provider implementation must satisfy."""

    def __init__(self):
        self.sent: list[dict] = []  # test-observable record of what was "sent"

    def send_reminder(self, *, phone: str, message: str) -> bool:
        entry = {"type": "reminder", "phone": phone, "message": message}
        self.sent.append(entry)
        log.info("REMINDER to %s: %s", phone, message)
        return True

    def send_handoff_alert(self, *, tenant_subdomain: str, customer_phone: str, context: str) -> bool:
        entry = {"type": "handoff", "tenant": tenant_subdomain, "phone": customer_phone, "context": context}
        self.sent.append(entry)
        log.info("HANDOFF requested for %s (%s): %s", tenant_subdomain, customer_phone, context)
        return True


_default_notifier: Notifier = LoggingNotifier()


def get_notifier() -> Notifier:
    return _default_notifier


def set_notifier(notifier: Notifier) -> None:
    """Test hook / future real-provider swap point."""
    global _default_notifier
    _default_notifier = notifier
