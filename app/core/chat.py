"""
Chat orchestration (docs/06-development-passes.md, Pass 2: bilingual chat,
groundedness, human handoff). Extends the existing single-tenant
prompt.py/llm.py pattern (bilingual system-prompt construction, provider-
agnostic completion call) rather than replacing it — those two modules are
reused as-is; this module adds the tenant-scoped grounding, persistence,
and fallback/handoff logic on top.

Deliberately NOT here: the LLM is never asked to invent or commit a
booking transaction. Booking is a structured flow (app/core/booking.py)
driven by explicit quick-actions/API calls once intent is detected — the
model answers questions and detects intent, it does not fabricate
appointment details. This is a safety boundary, not a missing feature.
"""
from sqlalchemy.orm import Session

from app import llm
from app.core.audit import write_audit_log
from app.core.notify import get_notifier
from app.tenant.models import AppConfig, KnowledgeBase, Conversation, Message, Service, Staff

# The model is instructed to emit this exact marker as the first token of
# its reply when it cannot answer from the grounding material provided.
# Checked and stripped before the reply ever reaches the customer — the
# customer never sees the marker itself, only the resulting handoff offer.
_FALLBACK_MARKER = "[[NO_MATCH]]"


def _get_config(session: Session) -> AppConfig:
    config = session.query(AppConfig).first()
    if config is None:
        config = AppConfig()
        session.add(config)
        session.flush()
    return config


def build_system_prompt(session: Session, language: str) -> str:
    config = _get_config(session)
    persona = config.persona_name or "the assistant"
    tone_text = f"\n\nTONE & PERSONA: {config.tone}" if config.tone else ""

    services = session.query(Service).filter_by(active=True).all()
    if services:
        service_lines = "\n".join(
            f"- {s.name} ({s.duration_minutes} min, {s.price} KD)" for s in services
        )
        services_text = f"\n\nSERVICES OFFERED:\n{service_lines}"
    else:
        services_text = ""

    staff = session.query(Staff).filter_by(active=True, calendar_connected=True).all()
    if staff:
        staff_lines = "\n".join(f"- {s.name}" + (f" ({s.gender})" if s.gender else "") for s in staff)
        staff_text = f"\n\nSTAFF AVAILABLE FOR BOOKING:\n{staff_lines}"
    else:
        staff_text = ""

    kb_docs = session.query(KnowledgeBase).filter_by(status="active").all()
    if kb_docs:
        docs_text = "\n\n".join(f"— {d.filename} —\n{d.content}" for d in kb_docs)
        kb_text = f"\n\nREFERENCE DOCUMENTS:\n{docs_text}"
    else:
        kb_text = ""

    base = (
        f"أنت {persona}، المساعد الذكي لهذا العمل. أجب عن الأسئلة بودّ واحترافية واختصار "
        f"بالاعتماد فقط على المعلومات الواردة أدناه. لا تختلق أي معلومة غير واردة هنا — "
        f"إذا لم تجد إجابة في المعلومات المتاحة، ابدأ ردّك بالضبط بالنص {_FALLBACK_MARKER} "
        f"متبوعًا برد مهذب يخبر الزائر أنك ستحيله لأحد الفريق."
        if language == "ar" else
        f"You are {persona}, the AI assistant for this business. Answer questions warmly, "
        f"professionally, and concisely, using ONLY the information provided below. Do not "
        f"invent anything not stated here. If you cannot answer from the information "
        f"available, start your reply with exactly {_FALLBACK_MARKER} followed by a polite "
        f"message telling the visitor you'll connect them with the team."
    )

    return f"{base}{services_text}{staff_text}{kb_text}{tone_text}"


async def handle_chat_message(
    session: Session,
    *,
    conversation: Conversation,
    user_message: str,
    provider: str,
    api_key: str,
    model: str,
    base_url: str,
    tenant_subdomain: str,
    customer_phone: str | None = None,
) -> dict:
    """
    Persists the user message, calls the LLM with tenant-scoped grounding,
    detects the fallback marker, persists the assistant reply (with
    was_fallback recorded), and triggers a handoff notification when
    grounding failed. Returns {"reply": str, "fallback": bool}.
    """
    session.add(Message(conversation_id=conversation.id, role="user", content=user_message))
    session.flush()

    history = (
        session.query(Message)
        .filter_by(conversation_id=conversation.id)
        .order_by(Message.id.asc())
        .all()
    )
    llm_messages = [{"role": m.role, "content": m.content} for m in history]

    system_prompt = build_system_prompt(session, conversation.language)

    raw_reply = await llm.chat_completion(
        provider=provider, api_key=api_key, model=model, base_url=base_url,
        system_prompt=system_prompt, messages=llm_messages,
    )

    is_fallback = raw_reply.strip().startswith(_FALLBACK_MARKER)
    reply_text = raw_reply.strip()
    if is_fallback:
        reply_text = reply_text[len(_FALLBACK_MARKER):].strip()

    session.add(Message(conversation_id=conversation.id, role="assistant",
                         content=reply_text, was_fallback=is_fallback))
    session.flush()

    if is_fallback:
        write_audit_log(
            session, actor="chat-bot", action="chat.fallback",
            target_type="conversation", target_id=conversation.id,
            detail={"question": user_message},
        )
        get_notifier().send_handoff_alert(
            tenant_subdomain=tenant_subdomain,
            customer_phone=customer_phone or "unknown",
            context=user_message,
        )

    return {"reply": reply_text, "fallback": is_fallback}


def unanswered_questions(session: Session, limit: int = 20) -> list[dict]:
    """
    Backs the Knowledge Base screen's "questions the assistant couldn't
    answer" panel (docs/11-tenant-admin-ui-spec.md) — the preceding user
    message for every fallback-marked assistant reply.
    """
    fallback_msgs = (
        session.query(Message)
        .filter_by(role="assistant", was_fallback=True)
        .order_by(Message.id.desc())
        .limit(limit)
        .all()
    )
    results = []
    for m in fallback_msgs:
        prev = (
            session.query(Message)
            .filter(Message.conversation_id == m.conversation_id, Message.id < m.id, Message.role == "user")
            .order_by(Message.id.desc())
            .first()
        )
        if prev:
            results.append({"question": prev.content, "asked_at": prev.created_at.isoformat()})
    return results
