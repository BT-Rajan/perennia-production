"""
Platform admin API (docs/10-platform-admin-ui-spec.md), Pass 2 scope only:
Tenants list, Tenant detail (Overview + Feature Flags tabs). The other
seven designed screens (Provisioning wizard UI, Migrations, Alerts,
Billing, Platform Users, both Audit Log viewers) remain deliberately
deferred — see docs/06-development-passes.md Pass 6, each with its own
named trigger.

Auth: deliberately reuses the EXISTING single-admin session mechanism
already in main.py (SESSION_COOKIE + verify_session_token, backed by
ADMIN_PASSWORD_HASH), rather than building a new platform_users table and
login flow. Pass 6 is explicit that a Platform Users screen — and by
extension, multi-user platform auth — is trigger-based ("a second platform
team member is hired"), not scheduled. Building a second auth system for a
team of one would violate the simplicity discipline (docs/08). When that
trigger fires, this dependency is the one place to swap.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.control.models import Tenant, FeatureFlag
from app.core.db import get_control_session
from app.core.feature_flags import PLAN_ENTITLEMENTS, set_feature_flag, FeatureNotEntitledError
from app.security import verify_session_token

router = APIRouter(prefix="/api/platform", tags=["platform-admin"])

SESSION_COOKIE = "perennia_session"  # matches app/main.py — same session, deliberately


def require_platform_admin(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail={"error": {"code": "not_authenticated", "message": "Please log in.", "details": {}}})
    data = verify_session_token(token)
    if not data:
        raise HTTPException(status_code=401, detail={"error": {"code": "session_expired", "message": "Session expired.", "details": {}}})
    return data


@router.get("/tenants")
def list_tenants(_admin: dict = Depends(require_platform_admin)):
    with get_control_session() as cs:
        tenants = cs.query(Tenant).order_by(Tenant.created_at.desc()).all()
        return [
            {
                "id": t.id, "subdomain": t.subdomain, "business_name": t.business_name,
                "status": t.status.value, "plan_tier": t.plan_tier.value,
                "created_at": t.created_at.isoformat(),
            }
            for t in tenants
        ]


@router.get("/tenants/{tenant_id}")
def tenant_detail(tenant_id: int, _admin: dict = Depends(require_platform_admin)):
    with get_control_session() as cs:
        tenant = cs.query(Tenant).filter_by(id=tenant_id).one_or_none()
        if tenant is None:
            raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": "Tenant not found.", "details": {}}})

        entitled_keys = sorted(PLAN_ENTITLEMENTS.get(tenant.plan_tier, set()))
        flags = {f.feature_key: f.enabled for f in cs.query(FeatureFlag).filter_by(tenant_id=tenant.id).all()}

        return {
            "id": tenant.id, "subdomain": tenant.subdomain, "business_name": tenant.business_name,
            "status": tenant.status.value, "plan_tier": tenant.plan_tier.value,
            "db_name": tenant.db_name, "created_at": tenant.created_at.isoformat(),
            "feature_flags": [
                {"key": k, "enabled": flags.get(k, False)} for k in entitled_keys
            ],
        }


@router.post("/tenants/{tenant_id}/flags/{feature_key}")
def toggle_flag(tenant_id: int, feature_key: str, enabled: bool, admin: dict = Depends(require_platform_admin)):
    with get_control_session() as cs:
        tenant = cs.query(Tenant).filter_by(id=tenant_id).one_or_none()
        if tenant is None:
            raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": "Tenant not found.", "details": {}}})
        try:
            flag = set_feature_flag(
                cs, tenant_id=tenant.id, plan_tier=tenant.plan_tier,
                feature_key=feature_key, enabled=enabled, updated_by=admin.get("u", "platform-admin"),
            )
        except FeatureNotEntitledError as e:
            raise HTTPException(status_code=403, detail={"error": {"code": "not_entitled", "message": str(e), "details": {}}})
        return {"feature_key": flag.feature_key, "enabled": flag.enabled}
