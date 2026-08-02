"""
Tenant-scoped admin login (docs/06-development-passes.md, Pass 1: "the one
piece of UI in this pass, since Pass 2 needs somewhere for an owner to log
in"). Deliberately minimal — just enough for a tenant admin to authenticate
against their own database's admin_user table.
"""
from fastapi import APIRouter, Depends, HTTPException, Response, Request
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings
from app.core.audit import write_audit_log
from app.core.tenant_context import get_current_tenant_ctx, TenantContext
from app.security import (
    verify_password, create_tenant_session_token, new_csrf_token,
)
from app.tenant.models import AdminUser

router = APIRouter(prefix="/api/tenant", tags=["tenant-auth"])
# Shares state with whichever app.state.limiter is set on the mounting
# FastAPI app (see main.py) — slowapi reads limits from app.state at
# request time, so this local instance only needs to match the same
# storage backend, not be the literal same object.
limiter = Limiter(key_func=get_remote_address)


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
@limiter.limit(settings.RATE_LIMIT_LOGIN)
def tenant_login(
    request: Request,
    body: LoginRequest,
    response: Response,
    ctx: TenantContext = Depends(get_current_tenant_ctx),
):
    admin = (
        ctx.tenant_session.query(AdminUser)
        .filter_by(username=body.username)
        .one_or_none()
    )
    if admin is None or not verify_password(body.password, admin.password_hash):
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "invalid_credentials", "message": "Incorrect username or password.", "details": {}}},
        )

    csrf = new_csrf_token()
    token = create_tenant_session_token(ctx.tenant.subdomain, admin.username, csrf)
    response.set_cookie(
        "perennia_tenant_session", token,
        httponly=True, secure=settings.COOKIE_SECURE, samesite="lax",
        max_age=settings.SESSION_TTL_SECONDS,
    )
    write_audit_log(
        ctx.tenant_session, actor=admin.username, action="admin.login",
        target_type="admin_user", target_id=admin.id, detail={},
    )
    return {"csrf_token": csrf, "tenant": ctx.tenant.subdomain}
