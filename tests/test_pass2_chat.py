"""
Pass 2 chat orchestration tests. The LLM call itself is monkeypatched —
this environment has no live provider credentials, and more importantly,
what needs testing here is the orchestration logic (grounding construction,
fallback detection, message persistence, handoff triggering), not whether
a particular LLM provider is reachable today.
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
from app.security import decrypt_secret
from app.tenant.models import AppConfig, Conversation, Message, Service, Staff, KnowledgeBase


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


class TestSystemPromptGrounding:
    def test_prompt_includes_services_staff_and_kb_bilingually(self):
        t = _provision("pytest-chat-alpha")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                ts.add(AppConfig(persona_name="Nora", tone="warm and brief"))
                facial = Service(name="Facial", duration_minutes=60, price=15)
                layla = Staff(name="Layla", gender="female", calendar_connected=True)
                layla.services.append(facial)
                ts.add_all([facial, layla])
                ts.add(KnowledgeBase(filename="hours.md", content="We are open 9am-9pm daily."))

            with get_tenant_session(t, pw) as ts:
                en_prompt = chat.build_system_prompt(ts, "en")
                ar_prompt = chat.build_system_prompt(ts, "ar")

            assert "Nora" in en_prompt
            assert "Facial" in en_prompt
            assert "Layla" in en_prompt
            assert "open 9am-9pm daily" in en_prompt
            assert chat._FALLBACK_MARKER in en_prompt

            assert "Nora" in ar_prompt
            assert chat._FALLBACK_MARKER in ar_prompt
            assert "أجب عن الأسئلة" in ar_prompt  # Arabic base prompt present
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)


class TestChatOrchestration:
    @pytest.mark.asyncio
    async def test_grounded_answer_persists_both_turns_no_handoff(self, monkeypatch):
        t = _provision("pytest-chat-beta")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)

            async def fake_chat_completion(**kwargs):
                return "We're open 9am-9pm daily."

            monkeypatch.setattr(chat.llm, "chat_completion", fake_chat_completion)

            with get_tenant_session(t, pw) as ts:
                convo = Conversation(channel="web", language="en")
                ts.add(convo)
                ts.flush()
                convo_id = convo.id

            with get_tenant_session(t, pw) as ts:
                convo = ts.query(Conversation).filter_by(id=convo_id).one()
                result = await chat.handle_chat_message(
                    ts, conversation=convo, user_message="What are your hours?",
                    provider="anthropic", api_key="fake-key", model="fake-model", base_url="",
                    tenant_subdomain=t.subdomain,
                )
            assert result["fallback"] is False
            assert "9am-9pm" in result["reply"]

            with get_tenant_session(t, pw) as ts:
                messages = ts.query(Message).filter_by(conversation_id=convo_id).order_by(Message.id).all()
            assert len(messages) == 2
            assert messages[0].role == "user"
            assert messages[1].role == "assistant"
            assert messages[1].was_fallback is False

            assert get_notifier().sent == []  # no handoff fired
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)

    @pytest.mark.asyncio
    async def test_fallback_marker_strips_and_triggers_handoff(self, monkeypatch):
        t = _provision("pytest-chat-gamma")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)

            async def fake_chat_completion(**kwargs):
                return f"{chat._FALLBACK_MARKER} Let me connect you with our team for that."

            monkeypatch.setattr(chat.llm, "chat_completion", fake_chat_completion)

            with get_tenant_session(t, pw) as ts:
                convo = Conversation(channel="web", language="en")
                ts.add(convo)
                ts.flush()
                convo_id = convo.id

            with get_tenant_session(t, pw) as ts:
                convo = ts.query(Conversation).filter_by(id=convo_id).one()
                result = await chat.handle_chat_message(
                    ts, conversation=convo, user_message="Do you offer wedding henna packages?",
                    provider="anthropic", api_key="fake-key", model="fake-model", base_url="",
                    tenant_subdomain=t.subdomain, customer_phone="+96500000009",
                )
            assert result["fallback"] is True
            assert chat._FALLBACK_MARKER not in result["reply"]  # stripped, customer never sees it
            assert "connect you" in result["reply"]

            with get_tenant_session(t, pw) as ts:
                messages = ts.query(Message).filter_by(conversation_id=convo_id).order_by(Message.id).all()
            assert messages[1].was_fallback is True

            with get_tenant_session(t, pw) as ts:
                from app.tenant.models import AuditLog
                audit_rows = ts.query(AuditLog).filter_by(action="chat.fallback").all()
            assert len(audit_rows) == 1

            notifier = get_notifier()
            handoffs = [e for e in notifier.sent if e["type"] == "handoff"]
            assert len(handoffs) == 1
            assert handoffs[0]["phone"] == "+96500000009"
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)

    @pytest.mark.asyncio
    async def test_unanswered_questions_surfaces_fallback_history(self, monkeypatch):
        t = _provision("pytest-chat-delta")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)

            async def fake_chat_completion(**kwargs):
                return f"{chat._FALLBACK_MARKER} Not sure, let me check with the team."

            monkeypatch.setattr(chat.llm, "chat_completion", fake_chat_completion)

            with get_tenant_session(t, pw) as ts:
                convo = Conversation(channel="web", language="en")
                ts.add(convo)
                ts.flush()
                convo_id = convo.id

            with get_tenant_session(t, pw) as ts:
                convo = ts.query(Conversation).filter_by(id=convo_id).one()
                await chat.handle_chat_message(
                    ts, conversation=convo, user_message="Do you have parking?",
                    provider="anthropic", api_key="fake-key", model="fake-model", base_url="",
                    tenant_subdomain=t.subdomain,
                )

            with get_tenant_session(t, pw) as ts:
                unanswered = chat.unanswered_questions(ts)
            assert len(unanswered) == 1
            assert unanswered[0]["question"] == "Do you have parking?"
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)
