"""
Customer reception API (docs/12-customer-reception-ui-spec.md). Public,
unauthenticated except for tenant resolution via the X-Tenant-Subdomain
header (or its production Host-based successor) — this is the surface a
tenant's own customers hit, not an admin surface.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings
from app.core import booking, chat as chat_module
from app.core.tenant_context import get_current_tenant_ctx, TenantContext
from app.security import decrypt_secret
from app.tenant.models import Service, Conversation, AppConfig, CreatedVia

router = APIRouter(prefix="/api/tenant/reception", tags=["customer-reception"])
limiter = Limiter(key_func=get_remote_address)


# ── Services & staff (landing screen "Services and prices" quick action) ─

@router.get("/services")
def list_services(ctx: TenantContext = Depends(get_current_tenant_ctx)):
    services = ctx.tenant_session.query(Service).filter_by(active=True).all()
    return [
        {"id": s.id, "name": s.name, "duration_minutes": s.duration_minutes,
         "price": float(s.price), "category": s.category}
        for s in services
    ]


@router.get("/staff")
def list_bookable_staff(
    service_id: int = Query(...),
    ctx: TenantContext = Depends(get_current_tenant_ctx),
):
    """Respects the calendar hard gate — see app/core/booking.py."""
    staff = booking.bookable_staff_for_service(ctx.tenant_session, service_id)
    return [{"id": s.id, "name": s.name, "gender": s.gender} for s in staff]


@router.get("/availability")
def get_availability(
    staff_id: int = Query(...),
    service_id: int = Query(...),
    date: str = Query(...),
    ctx: TenantContext = Depends(get_current_tenant_ctx),
):
    try:
        slots = booking.available_slots(ctx.tenant_session, staff_id, service_id, date)
    except booking.BookingError as e:
        raise HTTPException(status_code=400, detail={"error": {"code": "invalid_request", "message": str(e), "details": {}}})
    return {"slots": slots}


# ── Booking (docs/12, "In-chat booking flow") ────────────────────────────

class BookingRequest(BaseModel):
    customer_phone: str
    customer_name: str | None = None
    staff_id: int
    service_id: int
    start_iso: str
    language: str = "en"


@router.post("/booking")
def create_booking(
    body: BookingRequest,
    ctx: TenantContext = Depends(get_current_tenant_ctx),
):
    from app.core.feature_flags import is_feature_enabled

    deposit_required = is_feature_enabled(
        ctx.control_session, tenant_id=ctx.tenant.id, feature_key="booking.deposit"
    )
    service = ctx.tenant_session.query(Service).filter_by(id=body.service_id).one_or_none()
    if service is None:
        raise HTTPException(status_code=404, detail={"error": {"code": "service_not_found", "message": "Service not found.", "details": {}}})

    customer = booking.get_or_create_customer(
        ctx.tenant_session, phone=body.customer_phone, name=body.customer_name,
        language_pref=body.language,
    )
    try:
        appt = booking.create_appointment(
            ctx.tenant_session, customer=customer, staff_id=body.staff_id,
            service_id=body.service_id, start_iso=body.start_iso,
            deposit_required=deposit_required,
            # Pass 2 simplification: flat 5 KD deposit regardless of service
            # price. A per-service or percentage-based deposit amount is a
            # reasonable Pass 5+ enhancement, not needed to validate the
            # gating mechanism itself.
            deposit_amount=5 if deposit_required else None,
            created_via=CreatedVia.chat,
            actor=f"customer:{body.customer_phone}",
        )
    except booking.StaffNotBookableError as e:
        raise HTTPException(status_code=409, detail={"error": {"code": "staff_not_bookable", "message": str(e), "details": {}}})
    except booking.SlotUnavailableError as e:
        raise HTTPException(status_code=409, detail={"error": {"code": "slot_unavailable", "message": str(e), "details": {}}})

    return {
        "appointment_id": appt.id,
        "status": appt.status.value,
        "deposit_status": appt.deposit_status.value,
        "deposit_amount": float(appt.deposit_amount) if appt.deposit_amount else None,
    }


class DepositConfirmRequest(BaseModel):
    provider_ref: str


@router.post("/booking/{appointment_id}/confirm-deposit")
def confirm_deposit(
    appointment_id: int,
    body: DepositConfirmRequest,
    ctx: TenantContext = Depends(get_current_tenant_ctx),
):
    """
    Pass 2 simplification: this endpoint plays the role a real payment
    gateway's webhook would play (KNET/Stripe callback confirming a
    successful charge). No live gateway is wired in yet — see
    PASS1_README.md-equivalent note in the Pass 2 PR description. The
    booking-engine contract (confirm_deposit_payment) is provider-agnostic,
    so swapping in a real webhook handler here doesn't touch booking.py.
    """
    try:
        appt = booking.confirm_deposit_payment(
            ctx.tenant_session, appointment_id, provider_ref=body.provider_ref,
            actor="payment-gateway",
        )
    except booking.BookingError as e:
        raise HTTPException(status_code=400, detail={"error": {"code": "confirm_failed", "message": str(e), "details": {}}})
    return {"appointment_id": appt.id, "status": appt.status.value}


# ── Chat (docs/12, landing screen free-text + quick actions) ────────────

class ChatRequest(BaseModel):
    conversation_id: int | None = None
    message: str
    language: str = "en"
    customer_phone: str | None = None


@router.post("/chat")
@limiter.limit(settings.RATE_LIMIT_CHAT)
async def chat_endpoint(
    request: Request,
    body: ChatRequest,
    ctx: TenantContext = Depends(get_current_tenant_ctx),
):
    if body.conversation_id:
        convo = ctx.tenant_session.query(Conversation).filter_by(id=body.conversation_id).one_or_none()
        if convo is None:
            raise HTTPException(status_code=404, detail={"error": {"code": "conversation_not_found", "message": "Unknown conversation.", "details": {}}})
    else:
        convo = Conversation(channel="web", language=body.language)
        ctx.tenant_session.add(convo)
        ctx.tenant_session.flush()

    config = ctx.tenant_session.query(AppConfig).first()
    if config is None or not config.llm_api_key_encrypted:
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": "llm_not_configured",
                               "message": "This business hasn't finished setting up chat yet.",
                               "details": {}}},
        )
    api_key = decrypt_secret(config.llm_api_key_encrypted)

    result = await chat_module.handle_chat_message(
        ctx.tenant_session, conversation=convo, user_message=body.message,
        provider=config.llm_provider, api_key=api_key,
        model=config.llm_model or "claude-haiku-4-5-20251001",
        base_url=config.llm_base_url or "",
        tenant_subdomain=ctx.tenant.subdomain, customer_phone=body.customer_phone,
    )
    return {"conversation_id": convo.id, "reply": result["reply"], "fallback": result["fallback"]}
