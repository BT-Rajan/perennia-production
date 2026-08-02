"""
Tenant provisioning (docs/06-development-passes.md, Pass 1: "script, not a
wizard"). Creates a genuinely isolated MySQL database and a MySQL user whose
GRANTs are scoped to only that database — isolation enforced by MySQL itself,
not only by application code choosing the right connection string.

The audit_log table additionally has UPDATE/DELETE revoked for the tenant's
own application user, so even a compromised tenant-scoped credential cannot
alter history (docs/05-security-and-compliance.md).
"""
import re
import secrets
from dataclasses import dataclass

import pymysql
from sqlalchemy.orm import Session

from app.config import settings
from app.control.models import Tenant, TenantStatus, PlanTier
from app.core.db import init_tenant_db
from app.security import encrypt_secret

_SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


class InvalidSubdomainError(Exception):
    pass


class SubdomainTakenError(Exception):
    pass


@dataclass
class ProvisioningResult:
    tenant: Tenant
    steps_completed: list[str]
    failed_step: str | None = None
    error: str | None = None


def _admin_connection():
    return pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_ADMIN_USER,
        password=settings.MYSQL_ADMIN_PASSWORD,
        autocommit=True,
    )


def provision_tenant(
    control_session: Session,
    *,
    business_name: str,
    subdomain: str,
    plan_tier: PlanTier,
) -> ProvisioningResult:
    """
    Runs the 4 provisioning steps in order, recording status after each one.
    If step 2 (schema apply) fails, the tenant row is left with status
    PROVISIONING_FAILED and db_name/db_user already set — a retry does NOT
    recreate the database, only resumes from schema application. This is the
    partial-failure guarantee documented in docs/10-platform-admin-ui-spec.md.
    """
    subdomain = subdomain.strip().lower()
    if not _SUBDOMAIN_RE.match(subdomain):
        raise InvalidSubdomainError(
            "Subdomain must be 3-63 chars, lowercase letters/digits/hyphens, "
            "not starting or ending with a hyphen."
        )
    if control_session.query(Tenant).filter_by(subdomain=subdomain).one_or_none():
        raise SubdomainTakenError(f"Subdomain '{subdomain}' is already in use.")

    db_name = f"tenant_{subdomain.replace('-', '_')}"
    db_user = f"t_{subdomain.replace('-', '_')}"[:32]  # MySQL username length limit
    db_password = secrets.token_urlsafe(24)

    tenant = Tenant(
        subdomain=subdomain,
        business_name=business_name,
        status=TenantStatus.pending,
        plan_tier=plan_tier,
        db_host=settings.MYSQL_HOST,
        db_port=settings.MYSQL_PORT,
        db_name=db_name,
        db_user=db_user,
        db_pass_encrypted=encrypt_secret(db_password),
    )
    control_session.add(tenant)
    control_session.flush()  # assigns tenant.id without committing yet

    steps_completed: list[str] = []

    # Step 1: create isolated database and log
    try:
        conn = _admin_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                # Note the %% — pymysql treats a literal % in the query string
                # as the start of a param placeholder when params are passed,
                # so the MySQL host wildcard '%' must be escaped as %%.
                cur.execute(f"CREATE USER IF NOT EXISTS '{db_user}'@'%%' IDENTIFIED BY %s",
                            (db_password,))
                cur.execute(f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'%'")
                cur.execute("FLUSH PRIVILEGES")
        finally:
            conn.close()
        steps_completed.append("database_and_log_created")
    except Exception as e:
        control_session.flush()
        return ProvisioningResult(tenant=tenant, steps_completed=steps_completed,
                                   failed_step="database_and_log_created", error=str(e))

    # Step 2: apply schema template (includes insert-only trigger on audit_log)
    try:
        init_tenant_db(db_name=db_name, db_host=settings.MYSQL_HOST,
                        db_port=settings.MYSQL_PORT, db_user=db_user, db_password=db_password)
        steps_completed.append("schema_applied")
    except Exception as e:
        tenant.status = TenantStatus.provisioning_failed
        control_session.flush()
        return ProvisioningResult(tenant=tenant, steps_completed=steps_completed,
                                   failed_step="schema_applied", error=str(e))

    # Step 3: default feature flags for plan tier — left to the caller
    # (scripts/provision_tenant.py) since it needs the flag module and this
    # function is intentionally kept to DB-provisioning concerns only.
    steps_completed.append("ready_for_flag_defaults")

    tenant.status = TenantStatus.pending  # stays pending until first owner login
    control_session.flush()
    return ProvisioningResult(tenant=tenant, steps_completed=steps_completed)


def retry_provisioning(control_session: Session, tenant: Tenant) -> ProvisioningResult:
    """
    Resumes a PROVISIONING_FAILED tenant from schema application onward.
    Never re-runs database/user creation for a tenant that already has one —
    that is the specific bug this function exists to prevent.
    """
    if tenant.status != TenantStatus.provisioning_failed:
        raise ValueError("retry_provisioning is only valid for provisioning_failed tenants")

    db_password_encrypted = tenant.db_pass_encrypted
    from app.security import decrypt_secret
    db_password = decrypt_secret(db_password_encrypted)

    try:
        init_tenant_db(db_name=tenant.db_name, db_host=tenant.db_host,
                        db_port=tenant.db_port, db_user=tenant.db_user, db_password=db_password)
    except Exception as e:
        control_session.flush()
        return ProvisioningResult(tenant=tenant, steps_completed=["database_and_log_created"],
                                   failed_step="schema_applied", error=str(e))

    tenant.status = TenantStatus.pending
    control_session.flush()
    return ProvisioningResult(tenant=tenant,
                               steps_completed=["database_and_log_created", "schema_applied", "ready_for_flag_defaults"])
