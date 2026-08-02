"""
The one audit-logging function in the codebase (docs/05-security-and-compliance.md,
"Audit logging — universal, not selective"). Every mutating action calls this —
no feature writes its own audit-logging code.

Fail-closed: if the audit write itself fails, the caller's transaction should
not be considered successful for anything touching money, bookings, or
customer data. This function does not swallow exceptions.
"""
from typing import Any

from sqlalchemy.orm import Session

from app.tenant.models import AuditLog


def write_audit_log(
    session: Session,
    *,
    actor: str,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """
    Writes one audit entry within the caller's existing tenant session/
    transaction — it does not open or commit its own session, so the audit
    write and the action it records rise or fall together atomically.

    Never pass secrets (API keys, payment credentials, raw card data) in
    `detail` — see docs/05-security-and-compliance.md.
    """
    entry = AuditLog(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        detail=detail or {},
    )
    session.add(entry)
    session.flush()  # surfaces DB-level errors now, not at the caller's commit
    return entry
