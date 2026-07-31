"""
Google Calendar sync for appointment bookings.

Uses a service account (no browser OAuth flow, no admin login to Google
needed). The service account's own email must be added as an editor on
the target calendar. If GOOGLE_SERVICE_ACCOUNT_FILE / GOOGLE_CALENDAR_ID
are not set, every function here is a silent no-op — appointments still
get recorded locally, they just won't appear on a calendar until an
admin configures those two env vars.
"""
import datetime
import logging
import threading
from typing import Optional

from app.config import settings

log = logging.getLogger("perennia.gcal")

_service = None
_service_load_attempted = False
_lock = threading.Lock()


def _get_service():
    """Thread-safe Google Calendar service initialization with timeout configuration."""
    global _service, _service_load_attempted
    
    # Fast path: already initialized, no lock needed
    if _service is not None:
        return _service
    
    # Slow path: need to check/initialize with lock
    with _lock:
        # Double-check after acquiring lock
        if _service is not None:
            return _service
        if _service_load_attempted:
            return None
        _service_load_attempted = True

        if not settings.GOOGLE_SERVICE_ACCOUNT_FILE or not settings.GOOGLE_CALENDAR_ID:
            return None

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            import httplib2

            creds = service_account.Credentials.from_service_account_file(
                settings.GOOGLE_SERVICE_ACCOUNT_FILE,
                scopes=["https://www.googleapis.com/auth/calendar"],
            )
            # Configure timeout (10 seconds) to prevent indefinite hangs
            # If Google API is slow/unresponsive, fail quickly rather than blocking workers
            http = httplib2.Http(timeout=10)
            _service = build("calendar", "v3", credentials=creds, http=http, cache_discovery=False)
            return _service
        except Exception as e:
            log.warning("Google Calendar not available: %s", e)
            return None


def is_configured() -> bool:
    return _get_service() is not None


def fetch_busy_intervals(day_start: datetime.datetime, day_end: datetime.datetime) -> list[tuple[str, str]]:
    """Returns [(start_iso, end_iso), ...] busy blocks on the shared calendar for that window."""
    service = _get_service()
    if not service:
        return []
    try:
        body = {
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
            "items": [{"id": settings.GOOGLE_CALENDAR_ID}],
        }
        result = service.freebusy().query(body=body).execute()
        busy = result.get("calendars", {}).get(settings.GOOGLE_CALENDAR_ID, {}).get("busy", [])
        return [(b["start"], b["end"]) for b in busy]
    except Exception as e:
        log.warning("freebusy query failed: %s", e)
        return []


def create_event(
    *,
    summary: str,
    description: str,
    start: datetime.datetime,
    end: datetime.datetime,
    attendee_email: Optional[str] = None,
) -> Optional[str]:
    """Creates a calendar event, returns its event id, or None if unconfigured/failed."""
    service = _get_service()
    if not service:
        return None
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": settings.APPT_TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": settings.APPT_TIMEZONE},
    }
    if attendee_email:
        event["attendees"] = [{"email": attendee_email}]
    try:
        created = service.events().insert(
            calendarId=settings.GOOGLE_CALENDAR_ID, body=event, sendUpdates="all" if attendee_email else "none"
        ).execute()
        return created.get("id")
    except Exception as e:
        log.warning("Calendar event creation failed: %s", e)
        return None


def delete_event(event_id: str) -> bool:
    service = _get_service()
    if not service or not event_id:
        return False
    try:
        service.events().delete(calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        return True
    except Exception as e:
        log.warning("Calendar event deletion failed: %s", e)
        return False
