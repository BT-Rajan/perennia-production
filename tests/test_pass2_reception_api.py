"""
Customer reception API tests (docs/12-customer-reception-ui-spec.md).
Exercises the real HTTP layer via TestClient — LLM calls mocked for the
same reason as test_pass2_chat.py (no live provider credentials in this
environment; what needs testing is the endpoint/orchestration wiring).
"""
import sys
from pathlib import Path

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.control.models import PlanTier, Tenant
from app.control.provisioning import provision_tenant
from app.core.db import get_control_session, get_tenant_session
from app.core.feature_flags import set_feature_flag
from app.core.notify import LoggingNotifier, set_notifier
from app.security import decrypt_secret, encrypt_secret
from app.tenant.models import Service, Staff, AppConfig


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


@pytest.fixture(autouse=True)
def _reset_notifier():
    set_notifier(LoggingNotifier())
    yield


def _provision(subdomain: str, plan=PlanTier.growth) -> Tenant:
    with get_control_session() as cs:
        result = provision_tenant(cs, business_name=f"Test {subdomain}",
                                   subdomain=subdomain, plan_tier=plan)
        assert result.failed_step is None, f"provisioning failed: {result.error}"
        tenant_id = result.tenant.id
    with get_control_session() as cs:
        return cs.query(Tenant).filter_by(id=tenant_id).one()


class TestReceptionEndpoints:
    def test_services_staff_availability_and_booking_flow(self):
        t = _provision("pytest-reception-alpha", plan=PlanTier.growth)
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                facial = Service(name="Facial", duration_minutes=60, price=15)
                layla = Staff(name="Layla", gender="female", calendar_connected=True)
                layla.services.append(facial)
                ts.add_all([facial, layla])

            with get_control_session() as cs:
                set_feature_flag(cs, tenant_id=t.id, plan_tier=PlanTier.growth,
                                  feature_key="booking.deposit", enabled=True, updated_by="test")

            from fastapi.testclient import TestClient
            from app.main import app
            client = TestClient(app)
            headers = {"X-Tenant-Subdomain": t.subdomain}

            r = client.get("/api/tenant/reception/services", headers=headers)
            assert r.status_code == 200
            services = r.json()
            assert len(services) == 1
            service_id = services[0]["id"]

            r = client.get(f"/api/tenant/reception/staff?service_id={service_id}", headers=headers)
            assert r.status_code == 200
            staff_list = r.json()
            assert len(staff_list) == 1
            staff_id = staff_list[0]["id"]

            import datetime
            tomorrow = datetime.date.today() + datetime.timedelta(days=1)
            while tomorrow.weekday() >= 5:
                tomorrow += datetime.timedelta(days=1)

            r = client.get(
                f"/api/tenant/reception/availability?staff_id={staff_id}&service_id={service_id}&date={tomorrow.isoformat()}",
                headers=headers,
            )
            assert r.status_code == 200
            slots = r.json()["slots"]
            assert len(slots) > 0
            start_iso = slots[0]["start"]

            r = client.post("/api/tenant/reception/booking", headers=headers, json={
                "customer_phone": "+96500000010", "customer_name": "Fatima",
                "staff_id": staff_id, "service_id": service_id, "start_iso": start_iso,
            })
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "pending_deposit"  # deposit required, not confirmed yet
            appt_id = body["appointment_id"]

            r = client.post(f"/api/tenant/reception/booking/{appt_id}/confirm-deposit",
                             headers=headers, json={"provider_ref": "pay_test_abc"})
            assert r.status_code == 200
            assert r.json()["status"] == "confirmed"
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)

    def test_calendar_hard_gate_excludes_unconnected_staff_from_api(self):
        t = _provision("pytest-reception-beta")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                massage = Service(name="Massage", duration_minutes=90, price=20)
                yousef = Staff(name="Yousef", calendar_connected=False)
                yousef.services.append(massage)
                ts.add_all([massage, yousef])
                ts.flush()
                service_id = massage.id

            from fastapi.testclient import TestClient
            from app.main import app
            client = TestClient(app)
            headers = {"X-Tenant-Subdomain": t.subdomain}

            r = client.get(f"/api/tenant/reception/staff?service_id={service_id}", headers=headers)
            assert r.status_code == 200
            assert r.json() == []
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)

    def test_chat_endpoint_without_llm_configured_returns_clean_error(self):
        t = _provision("pytest-reception-gamma")
        try:
            from fastapi.testclient import TestClient
            from app.main import app
            client = TestClient(app)
            headers = {"X-Tenant-Subdomain": t.subdomain}

            r = client.post("/api/tenant/reception/chat", headers=headers,
                             json={"message": "What are your hours?"})
            assert r.status_code == 503
            assert r.json()["detail"]["error"]["code"] == "llm_not_configured"
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)

    def test_chat_endpoint_with_llm_configured_returns_reply(self, monkeypatch):
        t = _provision("pytest-reception-delta")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                ts.add(AppConfig(llm_provider="anthropic", llm_model="fake-model",
                                  llm_api_key_encrypted=encrypt_secret("fake-key")))

            from app.core import chat as chat_module

            async def fake_chat_completion(**kwargs):
                return "We're open 9am-9pm daily."
            monkeypatch.setattr(chat_module.llm, "chat_completion", fake_chat_completion)

            from fastapi.testclient import TestClient
            from app.main import app
            client = TestClient(app)
            headers = {"X-Tenant-Subdomain": t.subdomain}

            r = client.post("/api/tenant/reception/chat", headers=headers,
                             json={"message": "What are your hours?"})
            assert r.status_code == 200
            body = r.json()
            assert body["fallback"] is False
            assert "9am-9pm" in body["reply"]
            assert body["conversation_id"] is not None

            # Second turn, same conversation.
            r2 = client.post("/api/tenant/reception/chat", headers=headers,
                              json={"message": "And on Fridays?",
                                    "conversation_id": body["conversation_id"]})
            assert r2.status_code == 200
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)
