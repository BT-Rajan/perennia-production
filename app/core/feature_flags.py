"""
Feature flags: entitlement (does the plan include this) vs. enabled (has the
tenant turned it on). A tenant can only enable what they're entitled to —
enforced here, not just in a UI that could be bypassed by calling the API
directly.

Pass 1 scope: the data model and the guard function. No self-serve toggle
screen yet (docs/06-development-passes.md) — flags are set via
scripts/set_feature_flag.py or directly against the control DB.
"""
from sqlalchemy.orm import Session

from app.control.models import FeatureFlag, PlanTier

# Static plan -> feature-key entitlement map. Extended as new flags are
# introduced in later passes (docs/03-feature-flags.md has the full list).
PLAN_ENTITLEMENTS: dict[PlanTier, set[str]] = {
    PlanTier.starter: {
        "booking.multi_staff",
        "chat.bilingual",
        "notifications.reminders",
        "chat.human_handoff",
    },
    PlanTier.growth: {
        "booking.multi_staff",
        "chat.bilingual",
        "notifications.reminders",
        "chat.human_handoff",
        "booking.deposit",
        "booking.waitlist",
        "booking.recurring",
    },
    PlanTier.pro: {
        "booking.multi_staff",
        "chat.bilingual",
        "notifications.reminders",
        "chat.human_handoff",
        "booking.deposit",
        "booking.waitlist",
        "booking.recurring",
        "booking.gender_match",
        "booking.group",
        "retention.packages",
        "retention.loyalty",
    },
}


class FeatureNotEntitledError(Exception):
    """Raised when a tenant's plan doesn't include this feature at all."""


def is_entitled(plan_tier: PlanTier, feature_key: str) -> bool:
    return feature_key in PLAN_ENTITLEMENTS.get(plan_tier, set())


def set_feature_flag(
    control_session: Session,
    *,
    tenant_id: int,
    plan_tier: PlanTier,
    feature_key: str,
    enabled: bool,
    updated_by: str,
) -> FeatureFlag:
    """Enforces entitlement before allowing enabled=True."""
    if enabled and not is_entitled(plan_tier, feature_key):
        raise FeatureNotEntitledError(
            f"{feature_key} is not included in the {plan_tier.value} plan"
        )

    flag = (
        control_session.query(FeatureFlag)
        .filter_by(tenant_id=tenant_id, feature_key=feature_key)
        .one_or_none()
    )
    if flag is None:
        flag = FeatureFlag(tenant_id=tenant_id, feature_key=feature_key)
        control_session.add(flag)
    flag.enabled = enabled
    flag.updated_by = updated_by
    control_session.flush()
    return flag


def is_feature_enabled(control_session: Session, *, tenant_id: int, feature_key: str) -> bool:
    flag = (
        control_session.query(FeatureFlag)
        .filter_by(tenant_id=tenant_id, feature_key=feature_key)
        .one_or_none()
    )
    return bool(flag and flag.enabled)


def require_feature(feature_key: str):
    """
    FastAPI dependency factory. Usage:
        @app.post("/api/book")
        def book(..., _=Depends(require_feature("booking.deposit"))):
            ...
    Checks against the tenant resolved by app.core.tenant_context for this
    request. Raises the shared error shape (404-style "not enabled") rather
    than a generic 500 — see docs/04-api-conventions.md.
    """
    from fastapi import Depends, HTTPException
    from app.core.tenant_context import get_current_tenant_ctx

    def _dependency(ctx=Depends(get_current_tenant_ctx)):
        enabled = is_feature_enabled(
            ctx.control_session, tenant_id=ctx.tenant.id, feature_key=feature_key
        )
        if not enabled:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "feature_not_enabled",
                        "message": f"{feature_key} is not enabled for this account.",
                        "details": {},
                    }
                },
            )
        return True

    return _dependency
