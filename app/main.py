"""
Perennia backend.

Key architectural rule enforced throughout this file: the LLM API key
never appears in any HTTP response body sent to a browser, under any
route, at any time — not even to the authenticated admin. The admin
panel only ever sees a masked hint of the key it already saved.
"""
import datetime
import fcntl
import io
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
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
from app import storage, llm, extract, gcal, scheduling
from app import prompt as prompt_mod
from app.security import (
    verify_password, create_session_token, verify_session_token,
    new_csrf_token, csrf_tokens_match, mask_key, revoke_session_token,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("perennia")

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Perennia API", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Single-instance lock (prevents multi-instance data corruption) ────────
# Multi-instance deployments without a distributed lock will silently corrupt
# data. This check prevents that by enforcing single-instance deployment.
# For multi-instance, you would need to use Redis or a database for state.
_instance_lock_file = settings.DATA_DIR / ".instance_lock"
try:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _lock_fd = os.open(str(_instance_lock_file), os.O_CREAT | os.O_WRONLY, 0o666)
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        log.error(
            "FATAL: Another instance of Perennia is already running. "
            "Multi-instance deployment without a distributed lock is not supported. "
            "Each instance needs exclusive access to the data directory."
        )
        sys.exit(1)
except Exception as e:
    log.error("FATAL: Could not acquire instance lock: %s", e)
    sys.exit(1)

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
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
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
        return {"reply": limit_msg, "turnsUsed": turns_used, "maxTurns": max_turns, "limitReached": True}

    config = storage.load_config()
    api_key = storage.get_decrypted_api_key(config)

    if not api_key:
        fallback = (
            "لم يتم تكوين مفتاح API بعد. يرجى التواصل مع إدارة الموقع."
            if lang == "ar" else
            "API key not configured yet. Please contact the site administrator."
        )
        return {"reply": fallback, "turnsUsed": turns_used, "maxTurns": max_turns, "limitReached": False}

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

    return {
        "reply": reply,
        "turnsUsed": turns_used,
        "maxTurns": max_turns,
        "limitReached": turns_used >= max_turns,
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
    # Pre-check: slot should be in available slots (for UX, but not relied upon for correctness)
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
    
    # Log if calendar sync failed
    if not event_id:
        log.warning(
            "Calendar sync failed for appointment: %s <%s> on %s-%s",
            body.name, body.email, body.start, body.end
        )

    entry = {
        "id": uuid.uuid4().hex[:12],
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
    
    # Atomic: check availability and add appointment in single critical section
    try:
        storage.add_appointment(entry)
    except ValueError as e:
        # Slot was booked by another request between our check and write
        raise HTTPException(409, "That slot is no longer available. Please pick another.") from e
    
    storage.record_appointment_stat(start_dt.date().isoformat())

    return {
        "ok": True,
        "id": entry["id"],
        "start": entry["start"],
        "end": entry["end"],
        "calendarSynced": bool(event_id),
        "contactEmail": contact.get("ct-email", ""),
    }


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
        # Log failed login attempt for security audit
        client_ip = request.client.host if request.client else "unknown"
        log.warning("Failed admin login attempt from %s for user '%s'", client_ip, body.username.strip())
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
    
    # Log successful login for security audit
    client_ip = request.client.host if request.client else "unknown"
    log.info("Admin login successful for user '%s' from %s", body.username.strip(), client_ip)
    
    return {"ok": True, "csrfToken": csrf, "username": body.username.strip()}


@app.post("/api/admin/logout")
async def admin_logout(request: Request, response: Response):
    # Revoke the session token immediately to prevent reuse
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        revoke_session_token(token)
        log.info("Admin session revoked (logout)")
    
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


ALLOWED_PROVIDERS = {"anthropic", "deepseek", "openai", "custom"}


def _validate_custom_provider_url(url: str) -> str:
    """Validate custom LLM provider URL to prevent security issues.
    
    Rejects:
    - URLs without http/https scheme
    - URLs with private/internal IP addresses
    - Empty or malformed URLs
    
    Raises HTTPException(400) if invalid.
    """
    if not url or not url.strip():
        return ""  # Empty URL is OK (means use default)
    
    url = url.strip()
    try:
        parsed = urlparse(url)
        
        # Check scheme
        if parsed.scheme not in ("http", "https"):
            raise ValueError("URL must use http or https scheme")
        
        # Check for domain/netloc
        if not parsed.netloc:
            raise ValueError("URL must include a valid domain")
        
        # Check for private/internal IP addresses
        hostname = parsed.hostname or ""
        if hostname:
            # Check IPv4 private ranges
            if (hostname.startswith("127.") or 
                hostname.startswith("192.168.") or 
                hostname.startswith("10.") or
                hostname.startswith("172.") or
                hostname == "localhost"):
                raise ValueError("Private/internal IP addresses not allowed")
            
            # Check IPv6 loopback/private
            if hostname.startswith("["):  # IPv6
                ip_part = hostname.strip("[]")
                if ip_part.startswith("::1") or ip_part.startswith("fc"):
                    raise ValueError("Private/internal IPv6 addresses not allowed")
        
        return url[:500]  # Enforce length limit
    
    except ValueError as e:
        raise HTTPException(400, f"Invalid custom provider URL: {str(e)}")



def _public_config_view(config: dict) -> dict:
    api_key = storage.get_decrypted_api_key(config)
    view = {k: v for k, v in config.items() if k != "apiKeyEncrypted"}
    view["apiKeySet"] = bool(api_key)
    view["apiKeyMasked"] = mask_key(api_key)
    return view


@app.get("/api/admin/config")
async def get_config(session: dict = Depends(get_session)):
    return _public_config_view(storage.load_config())


@app.post("/api/admin/config")
async def update_config(body: ConfigUpdate, session: dict = Depends(require_csrf)):
    config = storage.load_config()
    changes = []

    if body.provider is not None:
        if body.provider not in ALLOWED_PROVIDERS:
            raise HTTPException(400, "Unknown provider.")
        if config.get("provider") != body.provider:
            changes.append(f"provider: {config.get('provider')} → {body.provider}")
            config["provider"] = body.provider
    if body.model is not None:
        new_model = body.model.strip()[:200]
        if config.get("model") != new_model:
            changes.append(f"model changed")
            config["model"] = new_model
    if body.baseUrl is not None:
        # Validate custom provider URL for security
        new_url = _validate_custom_provider_url(body.baseUrl)
        if config.get("baseUrl") != new_url:
            changes.append(f"baseUrl changed")
            config["baseUrl"] = new_url
    if body.tone is not None:
        new_tone = body.tone.strip()[:4000]
        if config.get("tone") != new_tone:
            changes.append("tone changed")
            config["tone"] = new_tone
    if body.knowledge is not None:
        new_knowledge = {str(k): str(v)[:20000] for k, v in body.knowledge.items()}
        if config.get("knowledge") != new_knowledge:
            changes.append("knowledge changed")
            config["knowledge"] = new_knowledge
    if body.contact is not None:
        new_contact = {str(k): str(v)[:500] for k, v in body.contact.items()}
        if config.get("contact") != new_contact:
            changes.append("contact information changed")
            config["contact"] = new_contact

    if body.clearApiKey:
        changes.append("API key cleared")
        config = storage.set_api_key(config, "")
    elif body.apiKey:
        changes.append("API key updated")
        config = storage.set_api_key(config, body.apiKey.strip())

    # Log config changes for audit trail
    if changes:
        log.info("Admin config updated: %s", ", ".join(changes))
    
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
@limiter.limit("10/hour")  # Max 10 uploads per hour to prevent DOS via disk exhaustion
async def upload_knowledge(request: Request, session: dict = Depends(require_csrf), file: UploadFile = File(...)):
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
        log.warning("Knowledge base upload failed for file '%s': %s", filename, error_msg)
        return JSONResponse(status_code=400, content={"ok": False, "error": error_msg})
    
    log.info("Knowledge base file uploaded: '%s' (%d chars)", filename, len(text))
    return {"ok": True, "id": entry["id"], "chars": entry["chars"], "truncated": truncated}


class DeleteKnowledgeRequest(BaseModel):
    id: str


@app.post("/api/admin/delete-knowledge")
async def delete_knowledge(body: DeleteKnowledgeRequest, session: dict = Depends(require_csrf)):
    entries = storage.load_knowledge_base()
    deleted = next((e for e in entries if e.get("id") == body.id), None)
    if deleted:
        log.info("Knowledge base file deleted: '%s'", deleted.get("filename"))
    
    entries = [e for e in entries if e.get("id") != body.id]
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
    if lang not in ("en", "ar"):
        raise HTTPException(400, "Invalid language.")
    raw = await logo.read()
    if len(raw) > settings.MAX_UPLOAD_IMAGE_BYTES:
        raise HTTPException(400, "File is too large (max 4 MB).")
    _save_as_png(raw, IMAGES_DIR / f"logo_{lang}.png")
    return {"ok": True}


@app.post("/api/admin/delete-logo")
async def delete_logo(body: dict, session: dict = Depends(require_csrf)):
    lang = body.get("lang")
    if lang not in ("en", "ar"):
        raise HTTPException(400, "Invalid language.")
    (IMAGES_DIR / f"logo_{lang}.png").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/admin/upload-avatar")
async def upload_avatar(session: dict = Depends(require_csrf), avatar: UploadFile = File(...)):
    raw = await avatar.read()
    if len(raw) > settings.MAX_UPLOAD_IMAGE_BYTES:
        raise HTTPException(400, "File is too large (max 4 MB).")
    _save_as_png(raw, IMAGES_DIR / "ai_avatar.png")
    return {"ok": True}


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
    found = False
    for a in entries:
        if a.get("id") == body.id and a.get("status") != "cancelled":
            found = True
            if a.get("calendar_event_id"):
                gcal.delete_event(a["calendar_event_id"])
            a["status"] = "cancelled"
    if not found:
        raise HTTPException(404, "Appointment not found.")
    storage.save_appointments(entries)
    return {"ok": True}


@app.get("/api/admin/analytics/daily")
async def analytics_daily(session: dict = Depends(get_session), days: int = 14):
    days = max(1, min(days, 90))
    return {
        "days": storage.daily_summary(days),
        "calendarConfigured": gcal.is_configured(),
        "maxTurns": settings.MAX_CHAT_EXCHANGES,
    }
