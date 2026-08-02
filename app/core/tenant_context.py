"""
Tenant resolution (docs/01-architecture.md, "Request flow").

This is the ONLY code path that may look up a tenant identifier and open a
connection to that tenant's database. Every other module receives an
already-resolved TenantContext — never a raw tenant_id from client input.

Resolution order: X-Tenant-Subdomain header (dev/API clients) first, then
Host-header subdomain parsing (browser clients hitting tenant.perennia.app)
in production. Kept header-based for now since local dev has no real
subdomains — swap/extend the resolver, not every call site, when real
subdomain routing is added.
"""
from dataclasses import dataclass

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from app.control.models import Tenant, TenantStatus
from app.core.db import get_control_session, get_tenant_session
from app.security import decrypt_secret


@dataclass
class TenantContext:
    tenant: Tenant
    control_session: Session
    tenant_session: Session


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"code": "tenant_not_found", "message": "Unknown tenant.", "details": {}}},
    )


def get_current_tenant_ctx(x_tenant_subdomain: str = Header(...)) -> TenantContext:
    """
    FastAPI dependency. Resolves the tenant, opens (or reuses, via the
    bounded pool) a session scoped to that tenant's own database, and hands
    both back. No route handler should accept a tenant identifier as a body
    or query parameter for the purpose of choosing which database to hit —
    this header (and its production Host-based successor) is the only input
    that determines that.
    """
    with get_control_session() as control_session:
        tenant = (
            control_session.query(Tenant)
            .filter_by(subdomain=x_tenant_subdomain)
            .one_or_none()
        )
        if tenant is None:
            raise _not_found()
        if tenant.status not in (TenantStatus.active, TenantStatus.pending):
            raise _not_found()  # suspended/cancelled tenants resolve as not-found, not a distinct error that leaks status

        db_password = decrypt_secret(tenant.db_pass_encrypted)

    # Note: control_session above is closed by its context manager on exit;
    # we captured what we need (tenant row, decrypted password) before that.
    with get_control_session() as control_session2, \
         get_tenant_session(tenant, db_password) as tenant_session:
        yield TenantContext(tenant=tenant, control_session=control_session2, tenant_session=tenant_session)
