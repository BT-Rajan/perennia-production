"""
Builds the system prompt for the visitor-facing assistant. This used to
run in the browser (and had to, since the browser held the API key and
called the provider directly). Now it runs here, server-side, and the
browser only ever sends/receives chat turns.
"""
from typing import Any

DEFAULT_KNOWLEDGE = """
COMPANY: Perennia — AI-powered technology and innovation company.
TAGLINE: "Solving Today. Shaping Tomorrow." (Arabic: حلول اليوم لصناعة الغد)
NAME ORIGIN: From Latin "Perennis" — lasting, enduring, resilient, continuously growing.
VISION: Trusted partner solving complex challenges through intelligent innovation.
MISSION: Practical, affordable AI solutions for today's challenges and tomorrow's opportunities.
VALUES: Intelligence · Innovation · Reliability · Progress · Impact

SOLUTIONS:
1. Perennia AI Business — AI Executive Dashboards, AI Knowledge Assistants, AI Research & Intelligence, AI Reporting Solutions
2. Perennia AI Automation — AI Workflow Automation, AI Document Automation, AI Communication Assistants, AI Data Collection Systems
3. Perennia AI Digital — AI-Powered Websites, AI-Powered Portals, AI-Powered Mobile Applications, SaaS Platforms
4. Perennia AI Personal — AI Financial Assistant, AI Productivity Assistant, AI Learning Assistant, AI Health & Performance Assistant

INDUSTRIES: Government, Energy & Oil and Gas, Manufacturing, Education, Healthcare, SMEs, Professionals, Entrepreneurs, Families

FLAGSHIP PRODUCTS:
• Perennia Insight™ — AI research & market intelligence platform
• Perennia Flow™ — AI automation platform
• Perennia Pulse™ — AI executive intelligence platform
• Perennia Personal™ — AI assistant for everyday life

WHY PERENNIA: AI-first, affordable innovation, practical solutions, scalable design, long-term value.
""".strip()


def _pick(knowledge: dict, for_lang: str, en_key: str, ar_key: str) -> str:
    if for_lang == "ar" and knowledge.get(ar_key):
        return knowledge[ar_key]
    return knowledge.get(en_key, "")


def build_knowledge_text(config: dict[str, Any], for_lang: str) -> str:
    k = config.get("knowledge") or {}
    about = _pick(k, for_lang, "ck-about-en", "ck-about-ar")
    vision = _pick(k, for_lang, "ck-vision-en", "ck-vision-ar")
    mission = _pick(k, for_lang, "ck-mission-en", "ck-mission-ar")
    solutions = _pick(k, for_lang, "ck-solutions-en", "ck-solutions-ar")
    products = k.get("ck-products-en", "")
    industries = k.get("ck-industries-en", "")

    if not any([about, vision, mission, solutions, products, industries]):
        return DEFAULT_KNOWLEDGE

    parts = []
    if about:
        parts.append(f"ABOUT: {about}")
    if vision:
        parts.append(f"VISION: {vision}")
    if mission:
        parts.append(f"MISSION: {mission}")
    if solutions:
        parts.append(f"SOLUTIONS:\n{solutions}")
    if products:
        parts.append(f"FLAGSHIP PRODUCTS:\n{products}")
    if industries:
        parts.append(f"INDUSTRIES: {industries}")
    return "\n\n".join(parts)


def build_nudge_text(for_lang: str, turns_used: int, max_turns: int) -> str:
    """Instructs the assistant to steer toward booking a call as the
    session's message budget runs low. Nothing shown to the visitor
    until the model naturally works it into a reply."""
    remaining = max_turns - turns_used
    if remaining > 3:
        return ""
    if for_lang == "ar":
        return (
            "\n\nملاحظة مهمة: تبقّت بضع رسائل فقط في هذه الجلسة. اختم إجابتك القادمة بدعوة "
            "لطيفة وغير مُلحّة لحجز موعد مع فريق بيرينيا لمناقشة التفاصيل مباشرة، دون أن تبدو "
            "وكأنك تتجاهل سؤال الزائر."
        )
    return (
        "\n\nIMPORTANT: only a few messages remain in this session. End your next reply with a "
        "brief, low-pressure invitation to book a short call with the Perennia team to continue "
        "the conversation — answer their question fully first, don't just deflect to booking."
    )


def build_system_prompt(
    config: dict[str, Any],
    knowledge_base: list[dict],
    for_lang: str,
    turns_used: int = 0,
    max_turns: int = 15,
) -> str:
    base = (
        "أنت المساعد الذكي الرسمي لشركة بيرينيا. أجب عن أسئلة الشركة بودّ واحترافية واختصار. "
        "استخدم فقرات قصيرة. لا تختلق معلومات غير واردة فيما يلي."
        if for_lang == "ar" else
        "You are the official AI assistant for PERENNIA. Answer questions about the company "
        "warmly, professionally, and concisely. Use short paragraphs. Do not invent information "
        "beyond what is provided below."
    )

    knowledge = build_knowledge_text(config, for_lang)

    kb_text = ""
    if knowledge_base:
        docs = "\n\n".join(f"— {f['filename']} —\n{f['text']}" for f in knowledge_base if f.get("ok"))
        if docs:
            kb_text = (
                "\n\nADDITIONAL REFERENCE DOCUMENTS (uploaded by the Perennia team — "
                "use these as supporting facts when relevant):\n" + docs
            )

    tone = config.get("tone") or ""
    tone_text = f"\n\nTONE & PERSONA INSTRUCTIONS: {tone}" if tone else ""

    contact_email = (config.get("contact") or {}).get("ct-email", "info@perennia.com")
    contact_line = (
        f"\n\nللاستفسار عن الأسعار أو الجداول الزمنية، أحِل الزائر إلى {contact_email}"
        if for_lang == "ar" else
        f"\n\nIf asked about pricing or timelines, invite visitors to contact {contact_email}."
    )

    nudge_text = build_nudge_text(for_lang, turns_used, max_turns)

    return f"{base}\n\n{knowledge}{kb_text}{tone_text}{contact_line}{nudge_text}"
