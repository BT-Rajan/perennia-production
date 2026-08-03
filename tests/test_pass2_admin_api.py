"""
Tenant admin panel API tests (docs/11, Pass 2 scope). Exercises the real
HTTP layer, real cookie-based session auth — not a bypassed/mocked auth
dependency, since the security property being tested (tenant resolved from
signed cookie, not client header) only means something if auth is real.
"""
import sys
from pathlib import Path

import bcrypt
import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.control.models import PlanTier, Tenant
from app.control.provisioning import provision_tenant
from app.core.db import get_control_session, get_tenant_session
from app.security import decrypt_secret
from app.tenant.models import AdminUser, Service, Staff


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


def _provision_with_admin(subdomain: str, username: str, password: str) -> Tenant:
    with get_control_session() as cs:
        result = provision_tenant(cs, business_name=f"Test {subdomain}",
                                   subdomain=subdomain, plan_tier=PlanTier.growth)
        assert result.failed_step is None, f"provisioning failed: {result.error}"
        tenant_id = result.tenant.id
    with get_control_session() as cs:
        tenant = cs.query(Tenant).filter_by(id=tenant_id).one()
    pw = decrypt_secret(tenant.db_pass_encrypted)
    with get_tenant_session(tenant, pw) as ts:
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        ts.add(AdminUser(username=username, password_hash=pw_hash))
    return tenant


def _logged_in_client(tenant_subdomain: str, username: str, password: str):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    r = client.post("/api/tenant/login", headers={"X-Tenant-Subdomain": tenant_subdomain},
                     json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return client


class TestAdminAuthRequired:
    def test_dashboard_requires_login(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        r = client.get("/api/tenant/admin/dashboard")
        assert r.status_code == 401


class TestDashboardAndBookings:
    def test_dashboard_and_bookings_reflect_real_data(self):
        t = _provision_with_admin("pytest-admin-alpha", "sara", "pw12345678")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                facial = Service(name="Facial", duration_minutes=60, price=15)
                layla = Staff(name="Layla", calendar_connected=True)
                layla.services.append(facial)
                ts.add_all([facial, layla])
                ts.flush()
                layla_id, facial_id = layla.id, facial.id

            from app.core import booking
            import datetime
            with get_tenant_session(t, pw) as ts:
                today_slots_date = datetime.date.today()
                # Force a booking "today" directly for a deterministic dashboard test
                # (available_slots only offers future days by design).
                customer = booking.get_or_create_customer(ts, phone="+96500000020", name="Huda")
                from app.tenant.models import Appointment, AppointmentStatus, DepositStatus, CreatedVia
                appt = Appointment(
                    customer_id=customer.id, staff_id=layla_id, service_id=facial_id,
                    start_time=datetime.datetime.combine(today_slots_date, datetime.time(10, 0)),
                    end_time=datetime.datetime.combine(today_slots_date, datetime.time(11, 0)),
                    status=AppointmentStatus.confirmed, deposit_status=DepositStatus.not_required,
                    created_via=CreatedVia.admin,
                )
                ts.add(appt)

            client = _logged_in_client("pytest-admin-alpha", "sara", "pw12345678")

            r = client.get("/api/tenant/admin/dashboard")
            assert r.status_code == 200
            body = r.json()
            assert body["todays_bookings"] == 1
            assert body["schedule"][0]["customer"] == "Huda"
            assert body["schedule"][0]["staff"] == "Layla"

            r = client.get(f"/api/tenant/admin/bookings?date={today_slots_date.isoformat()}")
            assert r.status_code == 200
            bookings = r.json()
            assert len(bookings) == 1
            assert bookings[0]["status"] == "confirmed"
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)


class TestStaffAndServices:
    def test_create_staff_and_service_then_connect_calendar(self):
        t = _provision_with_admin("pytest-admin-beta", "sara", "pw12345678")
        try:
            client = _logged_in_client("pytest-admin-beta", "sara", "pw12345678")

            r = client.post("/api/tenant/admin/services", json={
                "name": "Manicure", "duration_minutes": 30, "price": 8.0,
            })
            assert r.status_code == 200
            service_id = r.json()["id"]

            r = client.post("/api/tenant/admin/staff", json={
                "name": "Rania", "gender": "female", "service_ids": [service_id],
            })
            assert r.status_code == 200
            staff_id = r.json()["id"]

            r = client.get("/api/tenant/admin/staff")
            staff_list = r.json()
            assert staff_list[0]["calendar_connected"] is False  # not bookable until connected

            r = client.post(f"/api/tenant/admin/staff/{staff_id}/connect-calendar", json={"provider": "google"})
            assert r.status_code == 200
            assert r.json()["calendar_connected"] is True

            r = client.get("/api/tenant/admin/staff")
            assert r.json()[0]["calendar_connected"] is True
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)


class TestKnowledgeBase:
    def test_upload_list_and_delete_knowledge(self):
        t = _provision_with_admin("pytest-admin-gamma", "sara", "pw12345678")
        try:
            client = _logged_in_client("pytest-admin-gamma", "sara", "pw12345678")

            r = client.post("/api/tenant/admin/knowledge", json={
                "filename": "hours.md", "content": "Open 9am-9pm daily.",
            })
            assert r.status_code == 200
            doc_id = r.json()["id"]

            r = client.get("/api/tenant/admin/knowledge")
            assert len(r.json()) == 1
            assert r.json()[0]["filename"] == "hours.md"

            r = client.delete(f"/api/tenant/admin/knowledge/{doc_id}")
            assert r.status_code == 200

            r = client.get("/api/tenant/admin/knowledge")
            assert len(r.json()) == 0  # soft-deleted, not shown as active
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)


class TestCrossTenantSecurity:
    def test_admin_session_cannot_be_redirected_to_another_tenant_via_header(self):
        """
        The property that matters: a logged-in session for tenant A's admin
        route resolves tenant A regardless of what X-Tenant-Subdomain header
        is sent -- because admin routes read the tenant from the signed
        cookie (app/core/admin_context.py), not the header. If this test
        ever fails, it means an admin route started trusting the header,
        which would let an authenticated owner read/act on another tenant's
        data just by changing a header.
        """
        t1 = _provision_with_admin("pytest-admin-delta", "sara", "pw12345678")
        t2 = _provision_with_admin("pytest-admin-epsilon", "huda", "pw87654321")
        try:
            pw2 = decrypt_secret(t2.db_pass_encrypted)
            with get_tenant_session(t2, pw2) as ts:
                ts.add(Service(name="Secret Service", duration_minutes=30, price=99))

            client = _logged_in_client("pytest-admin-delta", "sara", "pw12345678")

            # Attempt to read tenant2's data by sending its header alongside
            # tenant1's valid session cookie.
            r = client.get("/api/tenant/admin/services", headers={"X-Tenant-Subdomain": t2.subdomain})
            assert r.status_code == 200
            names = [s["name"] for s in r.json()]
            assert "Secret Service" not in names  # tenant1's session, tenant1's (empty) services, header ignored
        finally:
            _cleanup_tenant(t1.subdomain, t1.db_name, t1.db_user)
            _cleanup_tenant(t2.subdomain, t2.db_name, t2.db_user)
