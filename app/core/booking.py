"""
Booking engine (docs/06-development-passes.md, Pass 2).

Mirrors the shape of the old single-tenant app/scheduling.py (business
hours, slot stepping, local-booking conflict check) but per-tenant and
per-staff, since Pass 2 introduces multi-staff booking. External calendar
sync (Google/Outlook) per staff is NOT implemented here — Staff.calendar_ref
exists in the schema for it, but live OAuth/service-account integration
needs real credentials this environment doesn't have. What IS implemented
and enforced: the hard gate that a staff member without calendar_connected
is never returned as bookable — see docs/11-tenant-admin-ui-spec.md.
"""
import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.core.audit import write_audit_log
from app.core.notify import get_notifier
from app.tenant.models import (
    AppConfig, Staff, Service, Customer, Appointment,
    AppointmentStatus, DepositStatus, CreatedVia,
)


class BookingError(Exception):
    pass


class StaffNotBookableError(BookingError):
    """Raised when a staff member without a connected calendar is requested."""


class SlotUnavailableError(BookingError):
    pass


def _get_config(session: Session) -> AppConfig:
    config = session.query(AppConfig).first()
    if config is None:
        config = AppConfig()
        session.add(config)
        session.flush()
    return config


def bookable_staff_for_service(session: Session, service_id: int) -> list[Staff]:
    """
    The hard gate lives here: a staff member with calendar_connected=False
    is excluded unconditionally, regardless of whether they offer the
    service. This is the single choke point every booking path goes
    through — chat and admin-initiated booking both call this, so there's
    no second code path that could forget the gate.
    """
    service = session.query(Service).filter_by(id=service_id, active=True).one_or_none()
    if service is None:
        return []
    return [
        s for s in service.staff_members
        if s.active and s.calendar_connected
    ]


def available_slots(session: Session, staff_id: int, service_id: int, date_str: str) -> list[dict]:
    """
    Works entirely in naive local-tenant-time datetimes. MySQL's DATETIME
    column doesn't retain timezone info the way SQLAlchemy's
    DateTime(timezone=True) implies — a row re-read from the DB comes back
    tz-naive, which breaks comparison against a tz-aware in-memory value.
    Since a tenant operates in one configured timezone (config.timezone),
    there's no information lost by dropping tzinfo consistently at the
    boundary — "now" and business hours are computed tz-aware (to get the
    correct local wall-clock time), then stripped to naive before any
    comparison against or storage into the database.
    """
    config = _get_config(session)
    tz = ZoneInfo(config.timezone)

    staff = session.query(Staff).filter_by(id=staff_id).one_or_none()
    if staff is None or not staff.active or not staff.calendar_connected:
        return []  # hard gate applies here too, not just in the listing helper

    service = session.query(Service).filter_by(id=service_id, active=True).one_or_none()
    if service is None:
        return []

    try:
        day = datetime.date.fromisoformat(date_str)
    except ValueError:
        raise BookingError("Invalid date format, expected YYYY-MM-DD.")

    today = datetime.datetime.now(tz).date()
    if day < today:
        return []

    workdays = {int(x) for x in config.workdays.split(",") if x.strip() != ""}
    if day.weekday() not in workdays:
        return []

    # Naive from here on — local tenant wall-clock time.
    day_start = datetime.datetime.combine(day, datetime.time(config.business_start_hour, 0))
    day_end = datetime.datetime.combine(day, datetime.time(config.business_end_hour, 0))
    now = datetime.datetime.now(tz).replace(tzinfo=None)

    existing = (
        session.query(Appointment)
        .filter(
            Appointment.staff_id == staff_id,
            Appointment.status.in_([AppointmentStatus.pending_deposit, AppointmentStatus.confirmed]),
            Appointment.start_time >= day_start,
            Appointment.start_time < day_end,
        )
        .all()
    )
    booked_ranges = [
        (a.start_time.replace(tzinfo=None) if a.start_time.tzinfo else a.start_time,
         a.end_time.replace(tzinfo=None) if a.end_time.tzinfo else a.end_time)
        for a in existing
    ]

    duration = datetime.timedelta(minutes=service.duration_minutes)
    step = datetime.timedelta(minutes=config.slot_minutes)

    slots = []
    cursor = day_start
    while cursor + duration <= day_end:
        slot_end = cursor + duration
        is_past = cursor < now
        conflicts = any(cursor < b_end and slot_end > b_start for b_start, b_end in booked_ranges)
        if not is_past and not conflicts:
            slots.append({"start": cursor.isoformat(), "end": slot_end.isoformat()})
        cursor += step
    return slots


def get_or_create_customer(session: Session, *, phone: str, name: str | None = None,
                            language_pref: str = "en") -> Customer:
    customer = session.query(Customer).filter_by(phone=phone).one_or_none()
    if customer is None:
        customer = Customer(phone=phone, name=name, language_pref=language_pref)
        session.add(customer)
        session.flush()
    return customer


def create_appointment(
    session: Session,
    *,
    customer: Customer,
    staff_id: int,
    service_id: int,
    start_iso: str,
    deposit_required: bool,
    deposit_amount=None,
    created_via: CreatedVia = CreatedVia.admin,
    actor: str,
) -> Appointment:
    """
    Deposit gating: if deposit_required, the appointment is created with
    status=pending_deposit and is NOT considered confirmed — the acceptance
    criterion is "a booking cannot be confirmed without a successful deposit
    payment," which this enforces by never setting status=confirmed here.
    confirm_deposit_payment() is the only path to AppointmentStatus.confirmed
    when a deposit is required.
    """
    staff = session.query(Staff).filter_by(id=staff_id).one_or_none()
    if staff is None or not staff.active or not staff.calendar_connected:
        raise StaffNotBookableError(f"Staff {staff_id} is not bookable (inactive or no connected calendar).")

    service = session.query(Service).filter_by(id=service_id, active=True).one_or_none()
    if service is None:
        raise BookingError(f"Service {service_id} not found or inactive.")

    start = datetime.datetime.fromisoformat(start_iso)
    end = start + datetime.timedelta(minutes=service.duration_minutes)

    slots = available_slots(session, staff_id, service_id, start.date().isoformat())
    if not any(s["start"] == start.isoformat() for s in slots):
        raise SlotUnavailableError(f"{start_iso} is no longer available for this staff member.")

    appointment = Appointment(
        customer_id=customer.id,
        staff_id=staff_id,
        service_id=service_id,
        start_time=start,
        end_time=end,
        status=AppointmentStatus.pending_deposit if deposit_required else AppointmentStatus.confirmed,
        deposit_status=DepositStatus.pending if deposit_required else DepositStatus.not_required,
        deposit_amount=deposit_amount if deposit_required else None,
        created_via=created_via,
    )
    session.add(appointment)
    session.flush()

    write_audit_log(
        session, actor=actor, action="booking.created", target_type="appointment",
        target_id=appointment.id,
        detail={"staff_id": staff_id, "service_id": service_id, "start": start_iso,
                "deposit_required": deposit_required},
    )
    return appointment


def confirm_deposit_payment(session: Session, appointment_id: int, *, provider_ref: str, actor: str) -> Appointment:
    appointment = session.query(Appointment).filter_by(id=appointment_id).one_or_none()
    if appointment is None:
        raise BookingError(f"Appointment {appointment_id} not found.")
    if appointment.status != AppointmentStatus.pending_deposit:
        raise BookingError(f"Appointment {appointment_id} is not awaiting a deposit "
                            f"(status={appointment.status.value}).")

    appointment.status = AppointmentStatus.confirmed
    appointment.deposit_status = DepositStatus.paid
    session.flush()

    write_audit_log(
        session, actor=actor, action="payment.succeeded", target_type="appointment",
        target_id=appointment.id, detail={"provider_ref": provider_ref},
    )

    customer = session.query(Customer).filter_by(id=appointment.customer_id).one()
    notifier = get_notifier()
    notifier.send_reminder(
        phone=customer.phone,
        message=f"Your appointment is confirmed for {appointment.start_time.isoformat()}.",
    )
    return appointment


def fail_deposit_payment(session: Session, appointment_id: int, *, reason: str, actor: str) -> Appointment:
    """
    A failed payment does NOT confirm the appointment — it stays
    pending_deposit, awaiting retry. This is the negative-path test the
    acceptance criteria call for: no confirmed appointment without a
    successful payment.
    """
    appointment = session.query(Appointment).filter_by(id=appointment_id).one_or_none()
    if appointment is None:
        raise BookingError(f"Appointment {appointment_id} not found.")

    write_audit_log(
        session, actor=actor, action="payment.failed", target_type="appointment",
        target_id=appointment.id, detail={"reason": reason},
    )
    return appointment


def cancel_appointment(session: Session, appointment_id: int, *, actor: str) -> Appointment:
    appointment = session.query(Appointment).filter_by(id=appointment_id).one_or_none()
    if appointment is None:
        raise BookingError(f"Appointment {appointment_id} not found.")
    appointment.status = AppointmentStatus.cancelled
    session.flush()
    write_audit_log(
        session, actor=actor, action="booking.cancelled", target_type="appointment",
        target_id=appointment.id, detail={},
    )
    return appointment
