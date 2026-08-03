"""
Pass 3: LLM provider failover tests (docs/06). Both providers are
monkeypatched — same reasoning as test_pass2_chat.py: no live credentials,
and what needs testing is the failover/degraded-mode logic itself.
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
from app.core.notify import LoggingNotifier, set_notifier, get_notifier
from app.core import chat
from app import llm as llm_module
from app.security import decrypt_secret
from app.tenant.models import Conversation, Message


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


def _provision(subdomain: str) -> Tenant:
    with get_control_session() as cs:
        result = provision_tenant(cs, business_name=f"Test {subdomain}",
                                   subdomain=subdomain, plan_tier=PlanTier.growth)
        assert result.failed_step is None, f"provisioning failed: {result.error}"
        tenant_id = result.tenant.id
    with get_control_session() as cs:
        return cs.query(Tenant).filter_by(id=tenant_id).one()


class TestLLMFailover:
    @pytest.mark.asyncio
    async def test_secondary_provider_used_when_primary_fails(self, monkeypatch):
        t = _provision("pytest-failover-alpha")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)

            call_log = []

            async def fake_completion(**kwargs):
                call_log.append(kwargs["provider"])
                if kwargs["provider"] == "anthropic":
                    raise llm_module.LLMError("Primary provider timeout", 502)
                return "Fallback provider says: we're open 9-9."

            monkeypatch.setattr(chat.llm, "chat_completion", fake_completion)

            with get_tenant_session(t, pw) as ts:
                convo = Conversation(channel="web", language="en")
                ts.add(convo)
                ts.flush()
                convo_id = convo.id

            with get_tenant_session(t, pw) as ts:
                convo = ts.query(Conversation).filter_by(id=convo_id).one()
                result = await chat.handle_chat_message(
                    ts, conversation=convo, user_message="Hours?",
                    provider="anthropic", api_key="fake", model="m", base_url="",
                    tenant_subdomain=t.subdomain,
                    secondary_provider="openai", secondary_api_key="fake2",
                    secondary_model="m2", secondary_base_url="",
                )
            assert call_log == ["anthropic", "openai"]
            assert "Fallback provider" in result["reply"]
            assert result["fallback"] is False  # a successful secondary answer is not itself a fallback

            with get_tenant_session(t, pw) as ts:
                from app.tenant.models import AuditLog
                rows = ts.query(AuditLog).filter_by(action="chat.failover_used").all()
            assert len(rows) == 1
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)

    @pytest.mark.asyncio
    async def test_both_providers_failing_returns_honest_degraded_message_not_silent_failure(self, monkeypatch):
        t = _provision("pytest-failover-beta")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)

            async def always_fail(**kwargs):
                raise llm_module.LLMError("Down", 502)

            monkeypatch.setattr(chat.llm, "chat_completion", always_fail)

            with get_tenant_session(t, pw) as ts:
                convo = Conversation(channel="web", language="en")
                ts.add(convo)
                ts.flush()
                convo_id = convo.id

            with get_tenant_session(t, pw) as ts:
                convo = ts.query(Conversation).filter_by(id=convo_id).one()
                result = await chat.handle_chat_message(
                    ts, conversation=convo, user_message="Hours?",
                    provider="anthropic", api_key="fake", model="m", base_url="",
                    tenant_subdomain=t.subdomain, customer_phone="+96500000099",
                    secondary_provider="openai", secondary_api_key="fake2",
                    secondary_model="m2", secondary_base_url="",
                )
            assert result["fallback"] is True
            assert "trouble" in result["reply"].lower()

            with get_tenant_session(t, pw) as ts:
                from app.tenant.models import AuditLog
                rows = ts.query(AuditLog).filter_by(action="chat.llm_unavailable").all()
            assert len(rows) == 1

            notifier = get_notifier()
            handoffs = [e for e in notifier.sent if e["type"] == "handoff"]
            assert len(handoffs) == 1
            assert handoffs[0]["phone"] == "+96500000099"
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)

    @pytest.mark.asyncio
    async def test_no_secondary_configured_goes_straight_to_degraded_message(self, monkeypatch):
        t = _provision("pytest-failover-gamma")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)

            async def always_fail(**kwargs):
                raise llm_module.LLMError("Down", 502)
            monkeypatch.setattr(chat.llm, "chat_completion", always_fail)

            with get_tenant_session(t, pw) as ts:
                convo = Conversation(channel="web", language="en")
                ts.add(convo)
                ts.flush()
                convo_id = convo.id

            with get_tenant_session(t, pw) as ts:
                convo = ts.query(Conversation).filter_by(id=convo_id).one()
                result = await chat.handle_chat_message(
                    ts, conversation=convo, user_message="Hours?",
                    provider="anthropic", api_key="fake", model="m", base_url="",
                    tenant_subdomain=t.subdomain,
                )
            assert result["fallback"] is True
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)
