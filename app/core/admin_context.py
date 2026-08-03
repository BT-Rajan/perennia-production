"""
Admin-side tenant resolution — distinct from app/core/tenant_context.py's
public resolver on purpose.

The public reception resolver trusts X-Tenant-Subdomain because there's no
session yet — anyone can ask "what does tenant X's public menu look like."
An authenticated admin route must NOT trust a client-supplied header for
which tenant it's allowed to act on, or a logged-in owner could simply
change a header and read/write a different tenant's data. Instead, the
tenant subdomain is read from inside the signed, server-issued session
cookie (app/security.py verify_tenant_session_token) — a client cannot
forge or alter that value without invalidating the signature.
"""
from dataclasses import dataclass

from fastapi import Request, HTTPException
from sqlalchemy.orm import Session

from app.control.models import Tenant, TenantStatus
from app.core.db import get_control_session, get_tenant_session
from app.security import decrypt_secret, verify_tenant_session_token


@dataclass
class AdminContext:
    tenant: Tenant
    username: str
    control_session: Session
    tenant_session: Session


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"error": {"code": "not_authenticated", "message": "Please log in.", "details": {}}},
    )


def get_admin_context(request: Request) -> AdminContext:
    token = request.cookies.get("perennia_tenant_session")
    if not token:
        raise _unauthorized()

    payload = verify_tenant_session_token(token)
    if payload is None:
        raise _unauthorized()

    subdomain = payload.get("sub")
    username = payload.get("u")
    if not subdomain or not username:
        raise _unauthorized()

    with get_control_session() as control_session:
        tenant = control_session.query(Tenant).filter_by(subdomain=subdomain).one_or_none()
        if tenant is None or tenant.status not in (TenantStatus.active, TenantStatus.pending):
            raise _unauthorized()
        db_password = decrypt_secret(tenant.db_pass_encrypted)

    with get_control_session() as control_session2, \
         get_tenant_session(tenant, db_password) as tenant_session:
        yield AdminContext(tenant=tenant, username=username,
                            control_session=control_session2, tenant_session=tenant_session)
