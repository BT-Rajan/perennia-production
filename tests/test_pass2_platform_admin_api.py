"""
Platform admin API tests (docs/10, Pass 2 scope: Tenants list, Tenant
detail). Auth reuses the existing single-admin session (/api/admin/login)
— tested here as real HTTP calls, not mocked, since the whole point of
reusing it is that it's already a tested, working mechanism.
"""
import sys
from pathlib import Path

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.control.models import PlanTier, Tenant
from app.control.provisioning import provision_tenant
from app.core.db import get_control_session
from app.core.feature_flags import set_feature_flag


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


def _platform_admin_client():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    r = client.post("/api/admin/login", json={"username": settings.ADMIN_USERNAME, "password": "devpassword"})
    assert r.status_code == 200, r.text
    return client


class TestPlatformAuthRequired:
    def test_tenants_list_requires_login(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        r = client.get("/api/platform/tenants")
        assert r.status_code == 401


class TestTenantsListAndDetail:
    def test_list_and_detail_reflect_real_tenants(self):
        t = _provision("pytest-platform-alpha", plan=PlanTier.growth)
        try:
            client = _platform_admin_client()

            r = client.get("/api/platform/tenants")
            assert r.status_code == 200
            subdomains = [row["subdomain"] for row in r.json()]
            assert t.subdomain in subdomains

            r = client.get(f"/api/platform/tenants/{t.id}")
            assert r.status_code == 200
            body = r.json()
            assert body["business_name"] == f"Test {t.subdomain}"
            assert body["plan_tier"] == "growth"
            # Growth-tier entitled flags appear, all default-disabled (provisioning
            # via this direct path -- not the CLI script -- doesn't set defaults).
            keys = [f["key"] for f in body["feature_flags"]]
            assert "booking.deposit" in keys
            assert "retention.packages" not in keys  # not entitled at growth tier
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)

    def test_detail_404_for_unknown_tenant(self):
        client = _platform_admin_client()
        r = client.get("/api/platform/tenants/999999")
        assert r.status_code == 404


class TestFeatureFlagToggle:
    def test_toggle_within_entitlement_succeeds(self):
        t = _provision("pytest-platform-beta", plan=PlanTier.growth)
        try:
            client = _platform_admin_client()
            r = client.post(f"/api/platform/tenants/{t.id}/flags/booking.deposit?enabled=true")
            assert r.status_code == 200
            assert r.json()["enabled"] is True

            r = client.get(f"/api/platform/tenants/{t.id}")
            flag = next(f for f in r.json()["feature_flags"] if f["key"] == "booking.deposit")
            assert flag["enabled"] is True
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)

    def test_toggle_outside_entitlement_rejected(self):
        t = _provision("pytest-platform-gamma", plan=PlanTier.starter)
        try:
            client = _platform_admin_client()
            r = client.post(f"/api/platform/tenants/{t.id}/flags/retention.loyalty?enabled=true")
            assert r.status_code == 403
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)
