"""
Tenant database models (docs/02-database-schema.md).

Provisioned once per tenant, into that tenant's own isolated MySQL database.
Pass 1 scope only: app_config, audit_log, and a minimal admin_user table so
a tenant owner has somewhere to log in. Booking/staff/customer tables are
Pass 2 scope (docs/06-development-passes.md) — deliberately not built here.
"""
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, DateTime, Text, JSON
from sqlalchemy.orm import declarative_base

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
