"""
Pass 3: backup & restore drill (docs/06). A real mysqldump backup, restored
into a separate database (not the live tenant DB, so the drill doesn't
risk the tenant's actual data), with integrity verified by comparing row
counts before and after — and confirming the audit_log insert-only trigger
survived the round trip, since a restore that drops the trigger would
silently reopen the tampering hole Pass 1 closed.
"""
import sys
import tempfile
from pathlib import Path

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.control.models import PlanTier, Tenant
from app.control.provisioning import provision_tenant
from app.core.db import get_control_session, get_tenant_session
from app.core.backup import backup_tenant_db, restore_tenant_db, table_row_counts, BackupError
from app.security import decrypt_secret
from app.tenant.models import Staff, Service, Customer, AuditLog


def _admin_conn():
    return pymysql.connect(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT,
                            user=settings.MYSQL_ADMIN_USER, password=settings.MYSQL_ADMIN_PASSWORD,
                            autocommit=True)


def _cleanup_tenant(subdomain: str, db_name: str, db_user: str):
    conn = _admin_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
            cur.execute(f"DROP USER IF EXISTS '{db_user}'@'%'")
    finally:
        conn.close()


def _cleanup_extra_db(db_name: str):
    conn = _admin_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
    finally:
        conn.close()


def _provision(subdomain: str) -> Tenant:
    with get_control_session() as cs:
        result = provision_tenant(cs, business_name=f"Test {subdomain}",
                                   subdomain=subdomain, plan_tier=PlanTier.growth)
        assert result.failed_step is None, f"provisioning failed: {result.error}"
        tenant_id = result.tenant.id
    with get_control_session() as cs:
        return cs.query(Tenant).filter_by(id=tenant_id).one()


class TestBackupAndRestoreDrill:
    def test_restore_preserves_data_and_the_immutability_trigger(self):
        t = _provision("pytest-backup-alpha")
        drill_db_name = f"{t.db_name}_restoredrill"
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                svc = Service(name="Facial", duration_minutes=60, price=15)
                staff = Staff(name="Layla", calendar_connected=True)
                staff.services.append(svc)
                cust = Customer(phone="+96500000030", name="Fatima")
                ts.add_all([svc, staff, cust])
                ts.add(AuditLog(actor="test", action="test.seed", detail={}))

            before_counts = table_row_counts(t.db_host, t.db_port, t.db_user, pw, t.db_name)
            assert before_counts.get("staff", 0) == 1
            assert before_counts.get("service", 0) == 1
            assert before_counts.get("customer", 0) == 1
            assert before_counts.get("audit_log", 0) == 1

            with tempfile.TemporaryDirectory() as tmpdir:
                backup_path = backup_tenant_db(t, Path(tmpdir))
                assert backup_path.exists()
                assert backup_path.stat().st_size > 0

                restore_tenant_db(t, backup_path, target_db_name=drill_db_name)

            after_counts = table_row_counts(t.db_host, t.db_port, settings.MYSQL_ADMIN_USER,
                                             settings.MYSQL_ADMIN_PASSWORD, drill_db_name)
            assert after_counts == before_counts, "restored row counts must match the original exactly"

            # The trigger must have survived the dump/restore round trip —
            # otherwise a real disaster recovery would silently reopen the
            # audit_log tampering hole Pass 1 closed.
            conn = pymysql.connect(host=t.db_host, port=t.db_port,
                                    user=settings.MYSQL_ADMIN_USER, password=settings.MYSQL_ADMIN_PASSWORD,
                                    database=drill_db_name, autocommit=True)
            try:
                with conn.cursor() as cur:
                    with pytest.raises(pymysql.err.OperationalError) as exc_info:
                        cur.execute("UPDATE audit_log SET actor='tampered' WHERE id=1")
                    assert exc_info.value.args[0] == 1644
            finally:
                conn.close()
        finally:
            _cleanup_extra_db(drill_db_name)
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)

    def test_backup_of_nonexistent_tenant_db_fails_loudly(self):
        """A backup failure must be loud (raise), never silently produce an
        empty or missing file that looks like success."""
        from app.control.models import Tenant as TenantModel
        fake_tenant = TenantModel(
            id=999999, subdomain="does-not-exist", business_name="x",
            db_host=settings.MYSQL_HOST, db_port=settings.MYSQL_PORT,
            db_name="db_that_does_not_exist_xyz", db_user="root",
            db_pass_encrypted=__import__("app.security", fromlist=["encrypt_secret"]).encrypt_secret("wrongpass"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(BackupError):
                backup_tenant_db(fake_tenant, Path(tmpdir))
