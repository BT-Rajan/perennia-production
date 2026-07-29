"""
Computes bookable appointment slots for a given day: business hours,
minus whatever's already booked locally, minus whatever Google Calendar
reports as busy (if configured).
"""
import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app import storage, gcal


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.APPT_TIMEZONE)


def _parse_date(date_str: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(date_str)
    except ValueError:
        raise ValueError("Invalid date format, expected YYYY-MM-DD.")


def available_slots(date_str: str) -> list[dict]:
    tz = _tz()
    day = _parse_date(date_str)
    today = datetime.datetime.now(tz).date()

    if day < today:
        return []
    if (day - today).days > settings.APPT_MAX_DAYS_AHEAD:
        return []
    if day.weekday() not in settings.APPT_WORKDAYS:
        return []

    day_start = datetime.datetime.combine(day, datetime.time(settings.APPT_DAY_START_HOUR, 0), tzinfo=tz)
    day_end = datetime.datetime.combine(day, datetime.time(settings.APPT_DAY_END_HOUR, 0), tzinfo=tz)

    # Local bookings for that day (source of truth for our own double-booking check).
    booked = {
        (a["start"], a["end"])
        for a in storage.load_appointments()
        if a.get("status") != "cancelled" and a.get("start", "").startswith(date_str)
    }

    busy_ranges = []
    for start_iso, end_iso in gcal.fetch_busy_intervals(day_start, day_end):
        try:
            busy_ranges.append((
                datetime.datetime.fromisoformat(start_iso),
                datetime.datetime.fromisoformat(end_iso),
            ))
        except ValueError:
            continue

    now = datetime.datetime.now(tz)
    step = datetime.timedelta(minutes=settings.APPT_SLOT_MINUTES)
    slots = []
    cursor = day_start
    while cursor + step <= day_end:
        slot_end = cursor + step
        is_past = cursor < now
        is_locally_booked = (cursor.isoformat(), slot_end.isoformat()) in booked
        is_calendar_busy = any(cursor < b_end and slot_end > b_start for b_start, b_end in busy_ranges)
        if not is_past and not is_locally_booked and not is_calendar_busy:
            try:
                label = cursor.strftime("%-I:%M %p")
            except ValueError:
                label = cursor.strftime("%I:%M %p").lstrip("0")
            slots.append({"start": cursor.isoformat(), "end": slot_end.isoformat(), "label": label})
        cursor = slot_end
    return slots


def slot_is_available(start_iso: str, end_iso: str) -> bool:
    try:
        start = datetime.datetime.fromisoformat(start_iso)
    except ValueError:
        return False
    date_str = start.date().isoformat()
    for s in available_slots(date_str):
        if s["start"] == start_iso and s["end"] == end_iso:
            return True
    return False
