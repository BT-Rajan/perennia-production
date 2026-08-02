"""
Schema migration orchestration across many tenant databases
(docs/02-database-schema.md, "Migration ownership"; docs/06 Pass 1: CLI, no UI).

Each migration is a (version, sql_statements) pair. Applying is idempotent
per tenant — status is tracked in control_session's MigrationLog so a
migration run can be stopped and resumed, and a single failing tenant can be
retried without re-running tenants that already succeeded.
"""
from dataclasses import dataclass

import pymysql
from sqlalchemy.orm import Session

from app.control.models import Tenant, TenantStatus, MigrationLog, MigrationStatus
from app.security import decrypt_secret

# Ordered list of migrations. Idempotency across re-runs is provided by
# per-tenant version tracking in MigrationLog (an already-`applied` version
# is skipped, never re-executed) — not by SQL-level IF NOT EXISTS clauses.
# Note: standard MySQL (unlike MariaDB) does not support IF NOT EXISTS on
# ADD COLUMN, so migration statements here must be plain, order-dependent
# DDL; don't add that clause expecting it to work.
MIGRATIONS: list[tuple[str, list[str]]] = [
    (
        "0001_app_config_notes",
        [
            "ALTER TABLE app_config ADD COLUMN notes TEXT NULL",
        ],
    ),
]


@dataclass
class TenantMigrationResult:
    tenant_id: int
    version: str
    status: MigrationStatus
    error: str | None = None


def _apply_to_tenant_db(tenant: Tenant, statements: list[str]) -> None:
    password = decrypt_secret(tenant.db_pass_encrypted)
    conn = pymysql.connect(
        host=tenant.db_host, port=tenant.db_port, user=tenant.db_user,
        password=password, database=tenant.db_name, autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    finally:
        conn.close()


def _get_or_create_log(control_session: Session, tenant_id: int, version: str) -> MigrationLog:
    row = (
        control_session.query(MigrationLog)
        .filter_by(tenant_id=tenant_id, migration_version=version)
        .one_or_none()
    )
    if row is None:
        row = MigrationLog(tenant_id=tenant_id, migration_version=version,
                            status=MigrationStatus.pending)
        control_session.add(row)
        control_session.flush()
    return row


def apply_migration_to_tenant(
    control_session: Session, tenant: Tenant, version: str, statements: list[str]
) -> TenantMigrationResult:
    from datetime import datetime, timezone

    log_row = _get_or_create_log(control_session, tenant.id, version)
    if log_row.status == MigrationStatus.applied:
        return TenantMigrationResult(tenant_id=tenant.id, version=version, status=MigrationStatus.applied)

    try:
        _apply_to_tenant_db(tenant, statements)
        log_row.status = MigrationStatus.applied
        log_row.applied_at = datetime.now(timezone.utc)
        log_row.error_message = None
        control_session.flush()
        return TenantMigrationResult(tenant_id=tenant.id, version=version, status=MigrationStatus.applied)
    except Exception as e:
        log_row.status = MigrationStatus.failed
        log_row.error_message = str(e)[:2000]
        control_session.flush()
        return TenantMigrationResult(tenant_id=tenant.id, version=version, status=MigrationStatus.failed, error=str(e))


def run_migration_across_tenants(
    control_session: Session, version: str, statements: list[str],
    only_tenant_ids: list[int] | None = None,
) -> list[TenantMigrationResult]:
    """
    Applies one migration to every active/pending tenant, or to a specific
    subset (used for retrying just the tenants that previously failed).
    """
    query = control_session.query(Tenant).filter(
        Tenant.status.in_([TenantStatus.active, TenantStatus.pending])
    )
    if only_tenant_ids is not None:
        query = query.filter(Tenant.id.in_(only_tenant_ids))

    results = []
    for tenant in query.all():
        results.append(apply_migration_to_tenant(control_session, tenant, version, statements))
    return results


def failed_tenant_ids_for(control_session: Session, version: str) -> list[int]:
    rows = (
        control_session.query(MigrationLog.tenant_id)
        .filter_by(migration_version=version, status=MigrationStatus.failed)
        .all()
    )
    return [r[0] for r in rows]
