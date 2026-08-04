"""
Perennia backend.

Key architectural rule enforced throughout this file: the LLM API key
never appears in any HTTP response body sent to a browser, under any
route, at any time — not even to the authenticated admin. The admin
panel only ever sees a masked hint of the key it already saved.
"""
import datetime
import io
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response, UploadFile, File, Form, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from PIL import Image

from app.config import settings
from app import storage, llm, extract, gcal, scheduling, notifications
from app import prompt as prompt_mod
from app.security import (
    verify_password, create_session_token, verify_session_token,
    new_csrf_token, csrf_tokens_match, mask_key,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("perennia")

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Perennia API", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

if settings.ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )

SESSION_COOKIE = "perennia_session"

# Static assets that are genuinely public (site pages, logos, avatar).
# `data/` (config + knowledge base) is never mounted here or anywhere else.
app.mount("/static", StaticFiles(directory=str(settings.PUBLIC_DIR / "static")), name="static")


# ── security response headers on every response ───────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none';"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ── auth dependency ─────────────────────────────────────────────────
def get_session(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    data = verify_session_token(token)
    if not data:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    return data


def require_csrf(request: Request, session: dict = Depends(get_session)) -> dict:
    header_token = request.headers.get("X-CSRF-Token")
    if not csrf_tokens_match(header_token, session.get("csrf")):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")
    return session


# ═══════════════════════════════════════════════════════════════════
# Public site pages
# ═══════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def serve_index():
    return FileResponse(settings.PUBLIC_DIR / "index.html")


@app.get("/admin")
def serve_admin():
    return FileResponse(settings.PUBLIC_DIR / "admin.html")


# ═══════════════════════════════════════════════════════════════════
# Public chat API — the only route that talks to the LLM provider
# ═══════════════════════════════════════════════════════════════════

class ChatTurn(BaseModel):
    role: str
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    lang: str = "en"
    message: str = Field(max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list)
    sessionId: str = Field(default="", max_length=64)


@app.post("/api/chat")
@limiter.limit(settings.RATE_LIMIT_CHAT)
async def chat(request: Request, body: ChatRequest):
    lang = "ar" if body.lang == "ar" else "en"
    max_turns = settings.MAX_CHAT_EXCHANGES
    turns_used = len([t for t in body.history if t.role == "user"]) + 1  # this message counts too

    session_id = re.sub(r"[^a-zA-Z0-9_-]", "", body.sessionId)[:64] or get_remote_address(request)
    today = datetime.datetime.now(ZoneInfo(settings.APPT_TIMEZONE)).date().isoformat()
    storage.record_interaction(today, session_id)

    if turns_used > max_turns:
        limit_msg = (
            "لقد وصلت إلى الحد الأقصى لعدد الرسائل في هذه الجلسة. يسعدنا مواصلة الحديث "
            "مباشرة — احجز موعداً سريعاً مع فريقنا."
            if lang == "ar" else
            "You've reached the message limit for this session. We'd love to keep the "
            "conversation going directly — please book a quick call with our team."
        )
        return {"reply": limit_msg, "turnsUsed": turns_used, "maxTurns": max_turns, "limitReached": True, "showBooking": True}

    config = storage.load_config()
    api_key = storage.get_decrypted_api_key(config)

    if not api_key:
        fallback = (
            "لم يتم تكوين مفتاح API بعد. يرجى التواصل مع إدارة الموقع."
            if lang == "ar" else
            "API key not configured yet. Please contact the site administrator."
        )
        return {"reply": fallback, "turnsUsed": turns_used, "maxTurns": max_turns, "limitReached": False, "showBooking": False}

    kb = storage.load_knowledge_base()
    system_prompt = prompt_mod.build_system_prompt(config, kb, lang, turns_used, max_turns)

    # Cap history so a visitor can't force unbounded token usage in one call.
    trimmed_history = [t.model_dump() for t in body.history[-20:]]
    messages = trimmed_history + [{"role": "user", "content": body.message}]

    try:
        reply = await llm.chat_completion(
            provider=config.get("provider", "anthropic"),
            api_key=api_key,
            model=config.get("model", "claude-sonnet-4-6"),
            base_url=config.get("baseUrl", ""),
            system_prompt=system_prompt,
            messages=messages,
        )
    except llm.LLMError as e:
        log.warning("LLM call failed: %s", e)
        raise HTTPException(status_code=e.status_code, detail="The assistant is temporarily unavailable.")

    # Determine if we should suggest booking
    booking_config = config.get("booking", {})
    booking_enabled = booking_config.get("enabled", True)
    show_booking = False
    booking_prompts = []
    trigger_booking_modal = False
    
    if booking_enabled and turns_used >= 3:  # Suggest booking after 3+ user turns
        # Keywords that suggest booking intent
        booking_keywords = {
            "en": ["book", "appointment", "call", "meeting", "schedule", "available", "time", "when", "how to", "interested"],
            "ar": ["احجز", "موعد", "مكالمة", "اجتماع", "جدول", "متاح", "الوقت", "متى", "كيف", "مهتم"],
        }
        
        affirmative_keywords = {
            "en": ["yes", "yeah", "sure", "okay", "ok", "please", "definitely", "absolutely"],
            "ar": ["نعم", "أجل", "حتما", "بالتأكيد", "من فضلك", "طبعا"],
        }
        
        user_msg_lower = body.message.lower()
        keywords = booking_keywords.get(lang, [])
        affirmative = affirmative_keywords.get(lang, [])
        
        # Check if user is asking about booking
        if any(keyword in user_msg_lower for keyword in keywords):
            show_booking = True
            booking_prompts = booking_config.get(f"prompts{lang.upper()}", [])
        
        # Check if this is a YES response to booking prompt (open modal)
        if turns_used >= 4 and len([t for t in body.history if "book" in t.content.lower() or "appointment" in t.content.lower()]) > 0:
            if any(aff in user_msg_lower for aff in affirmative):
                trigger_booking_modal = True

    return {
        "reply": reply,
        "turnsUsed": turns_used,
        "maxTurns": max_turns,
        "limitReached": turns_used >= max_turns,
        "showBooking": show_booking,
        "bookingPrompts": booking_prompts,
        "triggerBooking": trigger_booking_modal,
    }


# ═══════════════════════════════════════════════════════════════════
# Public: appointment booking — visitor-facing, no auth required.
# Availability is always recomputed server-side; the client can never
# force a double-booking by racing two requests (checked again at book
# time under the storage lock's write-then-read pattern).
# ═══════════════════════════════════════════════════════════════════

NAME_RE = re.compile(r"^[^\x00-\x1f<>]{1,120}$")
PHONE_RE = re.compile(r"^[0-9+()\-\s]{6,25}$")


@app.get("/api/appointments/availability")
@limiter.limit(settings.RATE_LIMIT_CHAT)
async def appointment_availability(request: Request, date: str):
    try:
        slots = scheduling.available_slots(date)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"date": date, "slots": slots}


class BookAppointmentRequest(BaseModel):
    name: str
    email: EmailStr
    phone: str = ""
    service: str = ""
    notes: str = Field(default="", max_length=1000)
    start: str
    end: str
    lang: str = "en"

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        v = v.strip()
        if not NAME_RE.match(v):
            raise ValueError("Invalid name.")
        return v

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v: str) -> str:
        v = v.strip()
        if v and not PHONE_RE.match(v):
            raise ValueError("Invalid phone number.")
        return v


@app.post("/api/appointments/book")
@limiter.limit(settings.RATE_LIMIT_APPOINTMENT)
async def book_appointment(request: Request, body: BookAppointmentRequest):
    # The availability check and the eventual write must be treated as one
    # unit — otherwise two requests can both pass the check for the same
    # slot before either has written its appointment, and double-book it.
    # A single process-wide lock is sufficient here since storage is local
    # JSON, not a shared DB (see the single-instance limitation noted in
    # the README).
    with storage.BOOKING_LOCK:
        if not scheduling.slot_is_available(body.start, body.end):
            raise HTTPException(409, "That slot is no longer available. Please pick another.")

        start_dt = datetime.datetime.fromisoformat(body.start)
        end_dt = datetime.datetime.fromisoformat(body.end)

        contact = (storage.load_config().get("contact") or {})
        summary = f"Perennia consultation — {body.name}"[:200]
        description = (
            f"Name: {body.name}\nEmail: {body.email}\nPhone: {body.phone or '—'}\n"
            f"Service interest: {body.service or '—'}\nNotes: {body.notes or '—'}"
        )

        event_id = gcal.create_event(
            summary=summary, description=description, start=start_dt, end=end_dt, attendee_email=body.email,
        )

        entry = {
            "id": f"PRN-{uuid.uuid4().hex[:8].upper()}",
            "name": body.name,
            "email": body.email,
            "phone": body.phone,
            "service": body.service,
            "notes": body.notes,
            "start": body.start,
            "end": body.end,
            "lang": "ar" if body.lang == "ar" else "en",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "calendar_event_id": event_id,
            "status": "confirmed",
        }
        storage.add_appointment(entry)
        storage.record_appointment_stat(start_dt.date().isoformat())

    # Email confirmations never block the booking response — SMTP can be
    # slow or briefly unreachable, and the appointment is already saved.
    threading.Thread(target=notifications.notify_booked, args=(entry,), daemon=True).start()

    return {
        "ok": True,
        "id": entry["id"],
        "start": entry["start"],
        "end": entry["end"],
        "calendarSynced": bool(event_id),
        "contactEmail": contact.get("ct-email", ""),
        "minNoticeHours": settings.APPT_MIN_NOTICE_HOURS,
    }


# ── Retrieve / reschedule / cancel — visitor-facing, identified by
# appointment ID + the email it was booked with (no login needed, but
# enough to keep a random ID guess from exposing someone else's booking).
# All three enforce the same minimum-notice window as reschedule/cancel.
# ═══════════════════════════════════════════════════════════════════

def _public_appt_view(a: dict) -> dict:
    return {
        "id": a["id"],
        "name": a.get("name", ""),
        "start": a["start"],
        "end": a["end"],
        "status": a.get("status", "confirmed"),
        "service": a.get("service", ""),
        "canModify": scheduling.can_modify(a),
        "minNoticeHours": settings.APPT_MIN_NOTICE_HOURS,
    }


def _find_appointment(entries: list[dict], appt_id: str, email: str) -> Optional[int]:
    appt_id = appt_id.strip()
    email = email.strip().lower()
    for i, a in enumerate(entries):
        if a.get("id") == appt_id and a.get("email", "").strip().lower() == email:
            return i
    return None


@app.get("/api/appointments/lookup")
@limiter.limit(settings.RATE_LIMIT_APPOINTMENT)
async def lookup_appointment(request: Request, id: str, email: EmailStr):
    entries = storage.load_appointments()
    idx = _find_appointment(entries, id, str(email))
    if idx is None:
        raise HTTPException(404, "No appointment found with that ID and email.")
    return _public_appt_view(entries[idx])


class RescheduleRequest(BaseModel):
    id: str
    email: EmailStr
    start: str
    end: str


@app.post("/api/appointments/reschedule")
@limiter.limit(settings.RATE_LIMIT_APPOINTMENT)
async def reschedule_appointment(request: Request, body: RescheduleRequest):
    with storage.BOOKING_LOCK:
        entries = storage.load_appointments()
        idx = _find_appointment(entries, body.id, str(body.email))
        if idx is None:
            raise HTTPException(404, "No appointment found with that ID and email.")
        entry = entries[idx]
        if entry.get("status") == "cancelled":
            raise HTTPException(409, "This appointment has already been cancelled.")
        if not scheduling.can_modify(entry):
            raise HTTPException(
                409,
                f"Changes must be made at least {settings.APPT_MIN_NOTICE_HOURS} hours "
                "before the appointment.",
            )
        if not scheduling.slot_is_available(body.start, body.end):
            raise HTTPException(409, "That slot is no longer available. Please pick another.")

        old_start = entry["start"]
        old_event_id = entry.get("calendar_event_id")
        start_dt = datetime.datetime.fromisoformat(body.start)
        end_dt = datetime.datetime.fromisoformat(body.end)

        summary = f"Perennia consultation — {entry['name']}"[:200]
        description = (
            f"Name: {entry['name']}\nEmail: {entry['email']}\nPhone: {entry.get('phone') or '—'}\n"
            f"Service interest: {entry.get('service') or '—'}\nNotes: {entry.get('notes') or '—'}"
        )
        new_event_id = gcal.create_event(
            summary=summary, description=description, start=start_dt, end=end_dt,
            attendee_email=entry["email"],
        )
        if old_event_id:
            gcal.delete_event(old_event_id)

        entry["start"] = body.start
        entry["end"] = body.end
        entry["calendar_event_id"] = new_event_id
        entry["rescheduled_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        entries[idx] = entry
        storage.save_appointments(entries)
        storage.record_appointment_stat(start_dt.date().isoformat())

    threading.Thread(target=notifications.notify_rescheduled, args=(entry, old_start), daemon=True).start()
    return _public_appt_view(entry)


class CancelPublicRequest(BaseModel):
    id: str
    email: EmailStr


@app.post("/api/appointments/cancel-request")
@limiter.limit(settings.RATE_LIMIT_APPOINTMENT)
async def cancel_appointment_public(request: Request, body: CancelPublicRequest):
    with storage.BOOKING_LOCK:
        entries = storage.load_appointments()
        idx = _find_appointment(entries, body.id, str(body.email))
        if idx is None:
            raise HTTPException(404, "No appointment found with that ID and email.")
        entry = entries[idx]
        if entry.get("status") == "cancelled":
            raise HTTPException(409, "This appointment has already been cancelled.")
        if not scheduling.can_modify(entry):
            raise HTTPException(
                409,
                f"Cancellations must be made at least {settings.APPT_MIN_NOTICE_HOURS} hours "
                "before the appointment.",
            )

        if entry.get("calendar_event_id"):
            gcal.delete_event(entry["calendar_event_id"])
        entry["status"] = "cancelled"
        entry["cancelled_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        entries[idx] = entry
        storage.save_appointments(entries)

    threading.Thread(target=notifications.notify_cancelled, args=(entry,), daemon=True).start()
    return _public_appt_view(entry)


# ═══════════════════════════════════════════════════════════════════
# Admin: auth
# ═══════════════════════════════════════════════════════════════════

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/admin/login")
@limiter.limit(settings.RATE_LIMIT_LOGIN)
async def admin_login(request: Request, response: Response, body: LoginRequest):
    valid = (
        body.username.strip() == settings.ADMIN_USERNAME
        and verify_password(body.password, settings.ADMIN_PASSWORD_HASH)
    )
    if not valid:
        # Generic message — never reveal whether the username or password was wrong.
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    csrf = new_csrf_token()
    token = create_session_token(body.username.strip(), csrf)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="strict",
        max_age=settings.SESSION_TTL_SECONDS,
        path="/",
    )
    return {"ok": True, "csrfToken": csrf, "username": body.username.strip()}


@app.post("/api/admin/logout")
async def admin_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/admin/session")
async def admin_session(session: dict = Depends(get_session)):
    return {"authenticated": True, "username": session["u"], "csrfToken": session["csrf"]}


# ═══════════════════════════════════════════════════════════════════
# Admin: config (provider/model/key/tone/knowledge/contact)
# ═══════════════════════════════════════════════════════════════════

class ConfigUpdate(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    baseUrl: Optional[str] = None
    apiKey: Optional[str] = None          # only present when admin typed a NEW key
    clearApiKey: Optional[bool] = False   # explicit removal
    tone: Optional[str] = None
    knowledge: Optional[dict] = None
    contact: Optional[dict] = None
    landing: Optional[dict] = None        # welcome text, tagline, nav links
    booking: Optional[dict] = None        # booking prompts and settings


ALLOWED_PROVIDERS = {"anthropic", "deepseek", "openai", "custom"}

# Landing page links (Our Work / Contact Us) go straight into
# `window.location.href` / `window.open` on the frontend with no further
# checking, so a scheme like `javascript:` or `data:` would execute in the
# visitor's browser. Only allow the schemes an admin would legitimately
# want here — everything else is rejected outright rather than silently
# stripped, so a mistyped URL fails loudly instead of quietly saving broken.
ALLOWED_LANDING_URL_SCHEMES = {"", "http", "https", "mailto", "tel"}


def _validate_landing_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    scheme = urlsplit(value).scheme.lower()
    if scheme not in ALLOWED_LANDING_URL_SCHEMES:
        raise HTTPException(
            400,
            f"Unsupported link type '{scheme}:' for this field. "
            "Use a web address (https://…), mailto:, tel:, or a relative path (/contact).",
        )
    return value


def _public_config_view(config: dict) -> dict:
    api_key = storage.get_decrypted_api_key(config)
    view = {k: v for k, v in config.items() if k != "apiKeyEncrypted"}
    view["apiKeySet"] = bool(api_key)
    view["apiKeyMasked"] = mask_key(api_key)
    return view


@app.get("/api/landing-config")
async def get_landing_config():
    """Public endpoint to fetch landing page configuration (welcome text, nav links, etc.)"""
    config = storage.load_config()
    return {
        "landing": config.get("landing", {}),
        "booking": config.get("booking", {}),
    }


@app.get("/api/admin/config")
async def get_config(session: dict = Depends(get_session)):
    return _public_config_view(storage.load_config())


@app.post("/api/admin/config")
async def update_config(body: ConfigUpdate, session: dict = Depends(require_csrf)):
    config = storage.load_config()

    if body.provider is not None:
        if body.provider not in ALLOWED_PROVIDERS:
            raise HTTPException(400, "Unknown provider.")
        config["provider"] = body.provider
    if body.model is not None:
        config["model"] = body.model.strip()[:200]
    if body.baseUrl is not None:
        config["baseUrl"] = body.baseUrl.strip()[:500]
    if body.tone is not None:
        config["tone"] = body.tone.strip()[:4000]
    if body.knowledge is not None:
        config["knowledge"] = {str(k): str(v)[:20000] for k, v in body.knowledge.items()}
    if body.contact is not None:
        config["contact"] = {str(k): str(v)[:500] for k, v in body.contact.items()}
    
    # Landing page config
    if body.landing is not None:
        landing = config.get("landing", {})
        for key, value in body.landing.items():
            if key in ["welcomeText-en", "welcomeText-ar", "tagline-en", "tagline-ar"]:
                landing[key] = str(value)[:500]
            elif key in ["ourWorkUrl", "contactUrl"]:
                landing[key] = _validate_landing_url(str(value)[:2000])
        config["landing"] = landing
    
    # Booking prompts config
    if body.booking is not None:
        booking = config.get("booking", {})
        if "promptsEn" in body.booking:
            booking["promptsEn"] = [str(p)[:500] for p in body.booking["promptsEn"]][:10]
        if "promptsAr" in body.booking:
            booking["promptsAr"] = [str(p)[:500] for p in body.booking["promptsAr"]][:10]
        if "enabled" in body.booking:
            booking["enabled"] = bool(body.booking["enabled"])
        config["booking"] = booking

    if body.clearApiKey:
        config = storage.set_api_key(config, "")
    elif body.apiKey:
        config = storage.set_api_key(config, body.apiKey.strip())

    storage.save_config(config)
    return _public_config_view(config)


class TestConnectionRequest(BaseModel):
    provider: str
    model: str = ""
    baseUrl: str = ""
    apiKey: Optional[str] = None  # test an unsaved key, or omit to test the saved one


@app.post("/api/admin/test-connection")
async def test_connection(body: TestConnectionRequest, session: dict = Depends(require_csrf)):
    key = body.apiKey.strip() if body.apiKey else storage.get_decrypted_api_key()
    ok, message = await llm.test_connection(
        provider=body.provider, api_key=key, model=body.model, base_url=body.baseUrl
    )
    return {"ok": ok, "message": message}


# ═══════════════════════════════════════════════════════════════════
# Admin: knowledge base files
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/admin/knowledge")
async def list_knowledge(session: dict = Depends(get_session)):
    entries = storage.load_knowledge_base()
    return [{k: v for k, v in e.items() if k != "text"} for e in entries]


@app.post("/api/admin/upload-knowledge")
async def upload_knowledge(session: dict = Depends(require_csrf), file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > settings.MAX_UPLOAD_DOC_BYTES:
        raise HTTPException(400, "File is too large (max 8 MB).")

    entries = storage.load_knowledge_base()
    if len(entries) >= settings.KB_MAX_TOTAL_ENTRIES:
        raise HTTPException(400, "Knowledge base is full. Remove a file before adding another.")

    filename = Path(file.filename or "upload").name  # strip any path components
    try:
        text, truncated = extract.extract_text(raw, filename)
        ok = True
    except extract.ExtractionError as e:
        text, truncated, ok = "", False, False
        error_msg = str(e)

    entry = {
        "id": uuid.uuid4().hex[:12],
        "filename": filename,
        "text": text,
        "chars": len(text),
        "ok": ok,
        "truncated": truncated,
    }
    entries.append(entry)
    storage.save_knowledge_base(entries)

    if not ok:
        return JSONResponse(status_code=400, content={"ok": False, "error": error_msg})
    return {"ok": True, "id": entry["id"], "chars": entry["chars"], "truncated": truncated}


class DeleteKnowledgeRequest(BaseModel):
    id: str


@app.post("/api/admin/delete-knowledge")
async def delete_knowledge(body: DeleteKnowledgeRequest, session: dict = Depends(require_csrf)):
    entries = [e for e in storage.load_knowledge_base() if e.get("id") != body.id]
    storage.save_knowledge_base(entries)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# Admin: logo / avatar uploads
#
# Uploaded images are re-encoded through Pillow before being written to
# disk. This is a deliberate defense-in-depth step: it strips any
# embedded scripts/metadata and guarantees the file really is the raster
# image it claims to be, regardless of what its extension or declared
# Content-Type said. SVG is intentionally not accepted, since inline SVG
# can carry <script> content — not worth the risk for a logo upload.
# ═══════════════════════════════════════════════════════════════════

IMAGES_DIR = settings.PUBLIC_DIR / "static" / "images"
MAX_IMAGE_DIMENSION = 2000


def _save_as_png(raw: bytes, dest: Path) -> None:
    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))  # reopen after verify()
    except Exception:
        raise HTTPException(400, "Unsupported or corrupt image file. Use PNG, JPEG, or WEBP.")

    if img.width > MAX_IMAGE_DIMENSION or img.height > MAX_IMAGE_DIMENSION:
        img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))

    if img.mode not in ("RGBA", "RGB"):
        img = img.convert("RGBA")

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")


@app.post("/api/admin/upload-logo")
async def upload_logo(
    session: dict = Depends(require_csrf),
    lang: str = Form(...),
    logo: UploadFile = File(...),
):
    """Upload and save logo image. Automatically reprocessed through Pillow
    for security (strips metadata/embedded scripts). Cache-buster timestamp
    included in response so client can refresh logo immediately."""
    if lang not in ("en", "ar"):
        raise HTTPException(400, "Invalid language.")
    raw = await logo.read()
    if len(raw) > settings.MAX_UPLOAD_IMAGE_BYTES:
        raise HTTPException(400, "File is too large (max 4 MB).")
    _save_as_png(raw, IMAGES_DIR / f"logo_{lang}.png")
    import time
    timestamp = int(time.time() * 1000)  # milliseconds for cache-busting
    return {"ok": True, "url": f"static/images/logo_{lang}.png?t={timestamp}"}


@app.post("/api/admin/delete-logo")
async def delete_logo(body: dict, session: dict = Depends(require_csrf)):
    lang = body.get("lang")
    if lang not in ("en", "ar"):
        raise HTTPException(400, "Invalid language.")
    (IMAGES_DIR / f"logo_{lang}.png").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/admin/upload-avatar")
async def upload_avatar(session: dict = Depends(require_csrf), avatar: UploadFile = File(...)):
    """Upload and save AI assistant avatar image. Automatically reprocessed
    through Pillow for security. Cache-buster timestamp included in response."""
    raw = await avatar.read()
    if len(raw) > settings.MAX_UPLOAD_IMAGE_BYTES:
        raise HTTPException(400, "File is too large (max 4 MB).")
    _save_as_png(raw, IMAGES_DIR / "ai_avatar.png")
    import time
    timestamp = int(time.time() * 1000)
    return {"ok": True, "url": f"static/images/ai_avatar.png?t={timestamp}"}


@app.post("/api/admin/delete-avatar")
async def delete_avatar(session: dict = Depends(require_csrf)):
    (IMAGES_DIR / "ai_avatar.png").unlink(missing_ok=True)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# Admin: appointments + daily interaction summary
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/admin/appointments")
async def list_appointments(session: dict = Depends(get_session)):
    entries = sorted(storage.load_appointments(), key=lambda a: a.get("start", ""), reverse=True)
    return entries


class CancelAppointmentRequest(BaseModel):
    id: str


@app.post("/api/admin/appointments/cancel")
async def cancel_appointment(body: CancelAppointmentRequest, session: dict = Depends(require_csrf)):
    entries = storage.load_appointments()
    found = None
    for a in entries:
        if a.get("id") == body.id and a.get("status") != "cancelled":
            found = a
            if a.get("calendar_event_id"):
                gcal.delete_event(a["calendar_event_id"])
            a["status"] = "cancelled"
            a["cancelled_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if found is None:
        raise HTTPException(404, "Appointment not found.")
    storage.save_appointments(entries)
    threading.Thread(target=notifications.notify_cancelled, args=(found,), daemon=True).start()
    return {"ok": True}


@app.get("/api/admin/analytics/daily")
async def analytics_daily(session: dict = Depends(get_session), days: int = 14):
    days = max(1, min(days, 90))
    return {
        "days": storage.daily_summary(days),
        "calendarConfigured": gcal.is_configured(),
        "maxTurns": settings.MAX_CHAT_EXCHANGES,
    }
