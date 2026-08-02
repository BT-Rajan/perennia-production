"""
Tenant database models (docs/02-database-schema.md).

Provisioned once per tenant, into that tenant's own isolated MySQL database.
Pass 1 tables: app_config, audit_log, admin_user.
Pass 2 tables (added below): staff, service, staff_service, customer,
appointment — the booking engine's data model (docs/06 Pass 2).
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, DateTime, Text, JSON, Boolean, Enum, ForeignKey,
    Numeric, Table, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

TenantBase = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AppConfig(TenantBase):
    """Single-row table. Persona/tone/branding config for this tenant."""
    __tablename__ = "app_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    persona_name = Column(String(255), nullable=True)
    tone = Column(String(64), nullable=True)
    language_default = Column(String(8), nullable=False, default="en")
    business_category = Column(String(64), nullable=True)  # drives discretion-mode default later
    timezone = Column(String(64), nullable=False, default="Asia/Kuwait")
    logo_ref = Column(String(255), nullable=True)
    # Pass 2: business hours the booking engine reads (per-tenant, not a
    # global settings default like the old single-tenant scheduling.py).
    business_start_hour = Column(Integer, nullable=False, default=9)
    business_end_hour = Column(Integer, nullable=False, default=17)
    workdays = Column(String(32), nullable=False, default="0,1,2,3,4")  # Mon=0 .. Sun=6
    slot_minutes = Column(Integer, nullable=False, default=30)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class AuditLog(TenantBase):
    """
    Insert-only. The application's MySQL user has no UPDATE/DELETE grant on
    this table (enforced in provisioning.py, not just by convention) — see
    docs/05-security-and-compliance.md.
    """
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor = Column(String(255), nullable=False)
    action = Column(String(128), nullable=False)
    target_type = Column(String(64), nullable=True)
    target_id = Column(String(64), nullable=True)
    detail = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class AdminUser(TenantBase):
    """
    Tenant-scoped admin credentials. Deliberately lives in the tenant's own
    database, not the control DB — a platform-level breach of the control DB
    does not hand over any tenant's login credentials.
    """
    __tablename__ = "admin_user"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


# ── Pass 2: booking engine (docs/06-development-passes.md) ──────────────

staff_service_table = Table(
    "staff_service", TenantBase.metadata,
    Column("staff_id", Integer, ForeignKey("staff.id"), primary_key=True),
    Column("service_id", Integer, ForeignKey("service.id"), primary_key=True),
)


class Staff(TenantBase):
    __tablename__ = "staff"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    gender = Column(String(16), nullable=True)  # first-class field, not an afterthought (docs/11)
    active = Column(Boolean, nullable=False, default=True)
    # Hard gate (docs/11 tenant-admin-ui-spec.md, Staff & Services): a staff
    # member without a connected calendar is never bookable. This flag is
    # the enforcement point, checked in booking.py — not just a UI warning.
    calendar_connected = Column(Boolean, nullable=False, default=False)
    calendar_provider = Column(String(32), nullable=True)  # 'google' | 'outlook'
    calendar_ref_encrypted = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    services = relationship("Service", secondary=staff_service_table, back_populates="staff_members")


class Service(TenantBase):
    __tablename__ = "service"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    price = Column(Numeric(10, 3), nullable=False)  # KD to 3 decimal places
    category = Column(String(128), nullable=True)
    active = Column(Boolean, nullable=False, default=True)

    staff_members = relationship("Staff", secondary=staff_service_table, back_populates="services")


class Customer(TenantBase):
    __tablename__ = "customer"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=True)
    phone = Column(String(32), nullable=False, unique=True)
    language_pref = Column(String(8), nullable=False, default="en")
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class AppointmentStatus(str, enum.Enum):
    pending_deposit = "pending_deposit"
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"
    no_show = "no_show"


class DepositStatus(str, enum.Enum):
    not_required = "not_required"
    pending = "pending"
    paid = "paid"
    refunded = "refunded"


class CreatedVia(str, enum.Enum):
    chat = "chat"
    admin = "admin"


class Appointment(TenantBase):
    __tablename__ = "appointment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customer.id"), nullable=False)
    staff_id = Column(Integer, ForeignKey("staff.id"), nullable=False)
    service_id = Column(Integer, ForeignKey("service.id"), nullable=False)
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    status = Column(Enum(AppointmentStatus), nullable=False, default=AppointmentStatus.pending_deposit)
    deposit_status = Column(Enum(DepositStatus), nullable=False, default=DepositStatus.not_required)
    deposit_amount = Column(Numeric(10, 3), nullable=True)
    created_via = Column(Enum(CreatedVia), nullable=False, default=CreatedVia.admin)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    customer = relationship("Customer")
    staff = relationship("Staff")
    service = relationship("Service")


# ── Pass 2: chat / knowledge base (docs/06-development-passes.md) ───────

class KnowledgeBase(TenantBase):
    __tablename__ = "knowledge_base"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=True)
    status = Column(String(16), nullable=False, default="active")  # 'active' | 'deleted'
    uploaded_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class Conversation(TenantBase):
    __tablename__ = "conversation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customer.id"), nullable=True)  # anonymous until identified
    channel = Column(String(16), nullable=False, default="web")
    language = Column(String(8), nullable=False, default="en")
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class Message(TenantBase):
    __tablename__ = "message"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, ForeignKey("conversation.id"), nullable=False)
    role = Column(String(16), nullable=False)  # 'user' | 'assistant'
    content = Column(Text, nullable=False)
    # True when the assistant could not ground an answer in the KB and a
    # handoff/fallback was triggered — this IS the "unanswered questions"
    # list surfaced on the Knowledge Base screen (docs/11).
    was_fallback = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
