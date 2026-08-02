"""
Pass 1 acceptance tests (docs/06-development-passes.md).
Runs against a real local MySQL — isolation and audit-log immutability are
guarantees that must be proven against the actual database engine, not
mocked away.

Requires: MySQL running locally with credentials from .env, and
`perennia_control` database existing (empty; these tests create/drop what
they need).
"""
import subprocess
import sys
from pathlib import Path

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.control.models import PlanTier, Tenant, TenantStatus
from app.control.provisioning import provision_tenant
from app.core.db import get_control_session, get_tenant_session, init_control_db
from app.core.feature_flags import (
    set_feature_flag, is_feature_enabled, is_entitled, FeatureNotEntitledError,
)
from app.core.audit import write_audit_log
from app.core.migrations import run_migration_across_tenants, failed_tenant_ids_for
from app.security import decrypt_secret
from app.tenant.models import AuditLog


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



def _provision(subdomain: str, plan=PlanTier.growth) -> Tenant:
    with get_control_session() as cs:
        result = provision_tenant(cs, business_name=f"Test {subdomain}",
                                   subdomain=subdomain, plan_tier=plan)
        assert result.failed_step is None, f"provisioning failed: {result.error}"
        tenant_id = result.tenant.id
    with get_control_session() as cs:
        return cs.query(Tenant).filter_by(id=tenant_id).one()


class TestProvisioningAndIsolation:
    def test_two_tenants_get_isolated_databases(self):
        t1 = _provision("pytest-alpha")
        t2 = _provision("pytest-beta")
        try:
            assert t1.db_name != t2.db_name
            assert t1.db_user != t2.db_user
        finally:
            _cleanup_tenant(t1.subdomain, t1.db_name, t1.db_user)
            _cleanup_tenant(t2.subdomain, t2.db_name, t2.db_user)

    def test_cross_tenant_access_is_denied_by_mysql_itself(self):
        """The acceptance criterion: an automated test proves cross-tenant
        access fails — not app-level assertion, an actual denied connection."""
        t1 = _provision("pytest-gamma")
        t2 = _provision("pytest-delta")
        try:
            pw1 = decrypt_secret(t1.db_pass_encrypted)
            with pytest.raises(pymysql.err.OperationalError) as exc_info:
                pymysql.connect(host=t1.db_host, port=t1.db_port, user=t1.db_user,
                                 password=pw1, database=t2.db_name)
            assert exc_info.value.args[0] == 1044  # access denied to database
        finally:
            _cleanup_tenant(t1.subdomain, t1.db_name, t1.db_user)
            _cleanup_tenant(t2.subdomain, t2.db_name, t2.db_user)


class TestAuditLog:
    def test_every_mutating_action_is_logged_and_immutable(self):
        t = _provision("pytest-epsilon")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                entry = write_audit_log(ts, actor="test-actor", action="test.action",
                                         target_type="thing", target_id="1", detail={"k": "v"})
                entry_id = entry.id

            # Confirm it landed.
            with get_tenant_session(t, pw) as ts:
                row = ts.query(AuditLog).filter_by(id=entry_id).one()
                assert row.actor == "test-actor"
                assert row.action == "test.action"

            # Confirm it cannot be altered, even via direct SQL with full
            # privileges on the tenant's own database.
            conn = pymysql.connect(host=t.db_host, port=t.db_port, user=t.db_user,
                                    password=pw, database=t.db_name, autocommit=True)
            try:
                with conn.cursor() as cur:
                    with pytest.raises(pymysql.err.OperationalError) as exc_info:
                        cur.execute(f"UPDATE audit_log SET actor='tampered' WHERE id={entry_id}")
                    assert exc_info.value.args[0] == 1644
                    with pytest.raises(pymysql.err.OperationalError) as exc_info:
                        cur.execute(f"DELETE FROM audit_log WHERE id={entry_id}")
                    assert exc_info.value.args[0] == 1644
            finally:
                conn.close()
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)


class TestFeatureFlags:
    def test_flag_toggled_for_one_tenant_does_not_affect_another(self):
        t1 = _provision("pytest-zeta", plan=PlanTier.growth)
        t2 = _provision("pytest-eta", plan=PlanTier.growth)
        try:
            with get_control_session() as cs:
                set_feature_flag(cs, tenant_id=t1.id, plan_tier=PlanTier.growth,
                                  feature_key="booking.deposit", enabled=True, updated_by="test")
            with get_control_session() as cs:
                assert is_feature_enabled(cs, tenant_id=t1.id, feature_key="booking.deposit") is True
                assert is_feature_enabled(cs, tenant_id=t2.id, feature_key="booking.deposit") is False
        finally:
            _cleanup_tenant(t1.subdomain, t1.db_name, t1.db_user)
            _cleanup_tenant(t2.subdomain, t2.db_name, t2.db_user)

    def test_cannot_enable_a_feature_outside_plan_entitlement(self):
        t = _provision("pytest-theta", plan=PlanTier.starter)
        try:
            assert is_entitled(PlanTier.starter, "retention.loyalty") is False
            with get_control_session() as cs:
                with pytest.raises(FeatureNotEntitledError):
                    set_feature_flag(cs, tenant_id=t.id, plan_tier=PlanTier.starter,
                                      feature_key="retention.loyalty", enabled=True, updated_by="test")
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)


class TestMigrationRunner:
    def test_migration_applies_and_one_failing_tenant_can_be_retried_independently(self):
        t1 = _provision("pytest-iota")
        t2 = _provision("pytest-kappa")
        try:
            version = "test_0001_add_col"
            good_sql = ["ALTER TABLE app_config ADD COLUMN test_col VARCHAR(10) NULL"]

            with get_control_session() as cs:
                results = run_migration_across_tenants(cs, version, good_sql, only_tenant_ids=[t1.id, t2.id])
            assert all(r.status.value == "applied" for r in results)

            # Now simulate t2 having failed a *different* migration version
            # (e.g. a bad statement), and prove retry only touches t2.
            bad_version = "test_0002_bad_statement"
            bad_sql = ["ALTER TABLE this_table_does_not_exist ADD COLUMN x INT"]
            with get_control_session() as cs:
                results = run_migration_across_tenants(cs, bad_version, bad_sql, only_tenant_ids=[t1.id, t2.id])
            assert all(r.status.value == "failed" for r in results)

            with get_control_session() as cs:
                failed_ids = failed_tenant_ids_for(cs, bad_version)
            assert set(failed_ids) == {t1.id, t2.id}

            # Retry with corrected SQL, only for t2 — t1's log entry for
            # this version should remain untouched by this call.
            fixed_sql = ["ALTER TABLE app_config ADD COLUMN test_col2 VARCHAR(10) NULL"]
            with get_control_session() as cs:
                retry_results = run_migration_across_tenants(cs, bad_version, fixed_sql, only_tenant_ids=[t2.id])
            assert len(retry_results) == 1
            assert retry_results[0].tenant_id == t2.id
            assert retry_results[0].status.value == "applied"

            with get_control_session() as cs:
                still_failed = failed_tenant_ids_for(cs, bad_version)
            assert still_failed == [t1.id]  # t1 untouched by the t2-only retry
        finally:
            _cleanup_tenant(t1.subdomain, t1.db_name, t1.db_user)
            _cleanup_tenant(t2.subdomain, t2.db_name, t2.db_user)


class TestTenantAdminLogin:
    def test_owner_can_log_in_scoped_to_own_tenant_only(self):
        import bcrypt
        from fastapi.testclient import TestClient
        from app.main import app
        from app.tenant.models import AdminUser

        t = _provision("pytest-login")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                pw_hash = bcrypt.hashpw(b"correcthorse123", bcrypt.gensalt()).decode()
                ts.add(AdminUser(username="owner", password_hash=pw_hash))

            client = TestClient(app)

            r = client.post("/api/tenant/login", headers={"X-Tenant-Subdomain": t.subdomain},
                             json={"username": "owner", "password": "wrongpass"})
            assert r.status_code == 401

            r = client.post("/api/tenant/login", headers={"X-Tenant-Subdomain": t.subdomain},
                             json={"username": "owner", "password": "correcthorse123"})
            assert r.status_code == 200
            assert "perennia_tenant_session" in r.cookies
            assert r.json()["tenant"] == t.subdomain

            r = client.post("/api/tenant/login", headers={"X-Tenant-Subdomain": "nonexistent-tenant"},
                             json={"username": "owner", "password": "correcthorse123"})
            assert r.status_code == 404

            # The login itself is audited.
            with get_tenant_session(t, pw) as ts:
                rows = ts.query(AuditLog).filter_by(action="admin.login").all()
                assert len(rows) == 1
                assert rows[0].actor == "owner"
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)


class TestAlerting:
    def test_threshold_breach_triggers_notification_call(self, monkeypatch):
        from app.core import alerting

        called = {}

        def fake_urlopen(req, timeout=5):
            called["url"] = req.full_url
            called["data"] = req.data

            class _Resp:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return _Resp()

        monkeypatch.setattr(alerting.settings, "ALERT_WEBHOOK_URL", "https://example.test/webhook")
        monkeypatch.setattr(alerting.urllib.request, "urlopen", fake_urlopen)

        fired = alerting.check_payment_failure_threshold(3, "pytest-tenant")
        assert fired is True
        assert called["url"] == "https://example.test/webhook"

    def test_no_webhook_configured_returns_false_not_exception(self, monkeypatch):
        from app.core import alerting
        monkeypatch.setattr(alerting.settings, "ALERT_WEBHOOK_URL", "")
        assert alerting.send_alert("test", "pytest-tenant", {}) is False
