"""
Tenant admin panel API (docs/11-tenant-admin-ui-spec.md), Pass 2 scope only:
Dashboard, Bookings/Calendar, Staff & Services, Knowledge Base. The other
five screens designed in docs/11 (Feature Flags self-serve, Analytics,
Audit Log viewer, Billing) are deliberately deferred — see
docs/06-development-passes.md Pass 6.

All routes here use get_admin_context (app/core/admin_context.py), which
resolves the tenant from the signed session cookie, not a client header.
"""
import datetime
import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.admin_context import get_admin_context, AdminContext
from app.core.audit import write_audit_log
from app.core.chat import unanswered_questions
from app.tenant.models import (
    Appointment, AppointmentStatus, DepositStatus, Staff, Service, Customer, KnowledgeBase,
)

router = APIRouter(prefix="/api/tenant/admin", tags=["tenant-admin"])


# ── Dashboard (docs/11, "Dashboard") ─────────────────────────────────────

@router.get("/dashboard")
def dashboard(ctx: AdminContext = Depends(get_admin_context)):
    ts = ctx.tenant_session
    today = datetime.date.today()
    today_start = datetime.datetime.combine(today, datetime.time.min)
    today_end = datetime.datetime.combine(today, datetime.time.max)

    todays_appointments = (
        ts.query(Appointment)
        .filter(Appointment.start_time >= today_start, Appointment.start_time <= today_end,
                Appointment.status.in_([AppointmentStatus.pending_deposit, AppointmentStatus.confirmed]))
        .order_by(Appointment.start_time)
        .all()
    )
    deposits_pending = sum(1 for a in todays_appointments if a.deposit_status == DepositStatus.pending)

    week_ago = datetime.datetime.now() - datetime.timedelta(days=7)
    no_shows_7d = (
        ts.query(Appointment)
        .filter(Appointment.status == AppointmentStatus.no_show, Appointment.start_time >= week_ago)
        .count()
    )

    unanswered = unanswered_questions(ts, limit=50)

    schedule = []
    for a in todays_appointments:
        schedule.append({
            "id": a.id,
            "time": a.start_time.strftime("%H:%M"),
            "customer": a.customer.name or a.customer.phone,
            "service": a.service.name,
            "staff": a.staff.name,
            "deposit_pending": a.deposit_status == DepositStatus.pending,
        })

    return {
        "todays_bookings": len(todays_appointments),
        "deposits_pending": deposits_pending,
        "unanswered_chat_questions": len(unanswered),
        "no_shows_7d": no_shows_7d,
        "schedule": schedule,
    }


# ── Bookings / Calendar (docs/11) ────────────────────────────────────────

@router.get("/bookings")
def list_bookings(date: str, ctx: AdminContext = Depends(get_admin_context)):
    try:
        day = datetime.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail={"error": {"code": "invalid_date", "message": "Expected YYYY-MM-DD.", "details": {}}})

    day_start = datetime.datetime.combine(day, datetime.time.min)
    day_end = datetime.datetime.combine(day, datetime.time.max)
    appts = (
        ctx.tenant_session.query(Appointment)
        .filter(Appointment.start_time >= day_start, Appointment.start_time <= day_end)
        .order_by(Appointment.start_time)
        .all()
    )
    return [
        {
            "id": a.id, "start": a.start_time.isoformat(), "end": a.end_time.isoformat(),
            "customer": a.customer.name or a.customer.phone, "customer_phone": a.customer.phone,
            "service": a.service.name, "staff": a.staff.name, "staff_id": a.staff_id,
            "status": a.status.value, "deposit_status": a.deposit_status.value,
            "created_via": a.created_via.value,
        }
        for a in appts
    ]


class CancelBookingRequest(BaseModel):
    reason: str | None = None


@router.post("/bookings/{appointment_id}/cancel")
def cancel_booking(appointment_id: int, body: CancelBookingRequest, ctx: AdminContext = Depends(get_admin_context)):
    from app.core import booking
    try:
        appt = booking.cancel_appointment(ctx.tenant_session, appointment_id, actor=ctx.username)
    except booking.BookingError as e:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": str(e), "details": {}}})
    return {"id": appt.id, "status": appt.status.value}


# ── Staff & Services (docs/11) ───────────────────────────────────────────

class StaffCreateRequest(BaseModel):
    name: str
    gender: str | None = None
    service_ids: list[int] = []


@router.get("/staff")
def list_staff(ctx: AdminContext = Depends(get_admin_context)):
    staff = ctx.tenant_session.query(Staff).all()
    return [
        {"id": s.id, "name": s.name, "gender": s.gender, "active": s.active,
         "calendar_connected": s.calendar_connected,
         "services": [sv.name for sv in s.services]}
        for s in staff
    ]


@router.post("/staff")
def create_staff(body: StaffCreateRequest, ctx: AdminContext = Depends(get_admin_context)):
    staff = Staff(name=body.name, gender=body.gender, calendar_connected=False)
    if body.service_ids:
        services = ctx.tenant_session.query(Service).filter(Service.id.in_(body.service_ids)).all()
        staff.services = services
    ctx.tenant_session.add(staff)
    ctx.tenant_session.flush()
    write_audit_log(ctx.tenant_session, actor=ctx.username, action="staff.created",
                     target_type="staff", target_id=staff.id, detail={"name": body.name})
    return {"id": staff.id}


class StaffCalendarConnectRequest(BaseModel):
    provider: str  # 'google' | 'outlook'


@router.post("/staff/{staff_id}/connect-calendar")
def connect_staff_calendar(staff_id: int, body: StaffCalendarConnectRequest, ctx: AdminContext = Depends(get_admin_context)):
    """
    Pass 2 simplification: marks the staff member as calendar-connected
    without a live OAuth flow (no real Google/Outlook credentials in this
    environment). This is the enforcement flag booking.py's hard gate
    actually checks — a real OAuth integration sets the same flag once a
    token exchange succeeds, without changing the gate logic itself.
    """
    staff = ctx.tenant_session.query(Staff).filter_by(id=staff_id).one_or_none()
    if staff is None:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": "Staff not found.", "details": {}}})
    staff.calendar_connected = True
    staff.calendar_provider = body.provider
    ctx.tenant_session.flush()
    write_audit_log(ctx.tenant_session, actor=ctx.username, action="staff.calendar_connected",
                     target_type="staff", target_id=staff.id, detail={"provider": body.provider})
    return {"id": staff.id, "calendar_connected": True}


class ServiceCreateRequest(BaseModel):
    name: str
    duration_minutes: int
    price: float
    category: str | None = None


@router.get("/services")
def list_services_admin(ctx: AdminContext = Depends(get_admin_context)):
    services = ctx.tenant_session.query(Service).all()
    return [
        {"id": s.id, "name": s.name, "duration_minutes": s.duration_minutes,
         "price": float(s.price), "category": s.category, "active": s.active}
        for s in services
    ]


@router.post("/services")
def create_service(body: ServiceCreateRequest, ctx: AdminContext = Depends(get_admin_context)):
    service = Service(name=body.name, duration_minutes=body.duration_minutes,
                       price=body.price, category=body.category)
    ctx.tenant_session.add(service)
    ctx.tenant_session.flush()
    write_audit_log(ctx.tenant_session, actor=ctx.username, action="service.created",
                     target_type="service", target_id=service.id, detail={"name": body.name})
    return {"id": service.id}


# ── Knowledge Base (docs/11) ─────────────────────────────────────────────

class KnowledgeUploadRequest(BaseModel):
    filename: str
    content: str


@router.get("/knowledge")
def list_knowledge(ctx: AdminContext = Depends(get_admin_context)):
    docs = ctx.tenant_session.query(KnowledgeBase).filter_by(status="active").all()
    return [
        {"id": d.id, "filename": d.filename, "uploaded_at": d.uploaded_at.isoformat()}
        for d in docs
    ]


@router.post("/knowledge")
def upload_knowledge(body: KnowledgeUploadRequest, ctx: AdminContext = Depends(get_admin_context)):
    content_hash = hashlib.sha256(body.content.encode()).hexdigest()
    doc = KnowledgeBase(filename=body.filename, content=body.content, content_hash=content_hash)
    ctx.tenant_session.add(doc)
    ctx.tenant_session.flush()
    write_audit_log(ctx.tenant_session, actor=ctx.username, action="knowledge_base.updated",
                     target_type="knowledge_base", target_id=doc.id, detail={"filename": body.filename})
    return {"id": doc.id}


@router.delete("/knowledge/{doc_id}")
def delete_knowledge(doc_id: int, ctx: AdminContext = Depends(get_admin_context)):
    doc = ctx.tenant_session.query(KnowledgeBase).filter_by(id=doc_id).one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": "Document not found.", "details": {}}})
    doc.status = "deleted"
    ctx.tenant_session.flush()
    write_audit_log(ctx.tenant_session, actor=ctx.username, action="knowledge_base.deleted",
                     target_type="knowledge_base", target_id=doc.id, detail={"filename": doc.filename})
    return {"id": doc.id, "status": "deleted"}


@router.get("/knowledge/unanswered")
def list_unanswered(ctx: AdminContext = Depends(get_admin_context)):
    return unanswered_questions(ctx.tenant_session, limit=50)
