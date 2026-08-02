"""
Control database models (docs/02-database-schema.md).

The control DB is a single, platform-owned database. It NEVER holds tenant
application data — only what the platform needs to know *about* tenants
(who they are, what plan they're on, which flags are enabled, migration
rollout status). Customer data lives exclusively in each tenant's own
database (app/tenant/models.py).
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Enum, Text, UniqueConstraint,
    ForeignKey, LargeBinary,
)
from sqlalchemy.orm import declarative_base

ControlBase = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TenantStatus(str, enum.Enum):
    pending = "pending"              # provisioning not yet complete
    active = "active"
    suspended = "suspended"
    cancelled = "cancelled"
    provisioning_failed = "provisioning_failed"


class PlanTier(str, enum.Enum):
    starter = "starter"
    growth = "growth"
    pro = "pro"


class Tenant(ControlBase):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subdomain = Column(String(63), unique=True, nullable=False, index=True)
    business_name = Column(String(255), nullable=False)
    status = Column(Enum(TenantStatus), nullable=False, default=TenantStatus.pending)
    plan_tier = Column(Enum(PlanTier), nullable=False, default=PlanTier.starter)

    # Per-tenant MySQL connection details. db_pass is Fernet-encrypted at
    # rest (app/security.py) — never stored or logged in plaintext.
    db_host = Column(String(255), nullable=False)
    db_port = Column(Integer, nullable=False, default=3306)
    db_name = Column(String(64), nullable=False, unique=True)
    db_user = Column(String(64), nullable=False)
    db_pass_encrypted = Column(Text, nullable=False)

    region = Column(String(32), nullable=False, default="kw")  # reserved for future multi-region

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class FeatureFlag(ControlBase):
    __tablename__ = "feature_flags"
    __table_args__ = (UniqueConstraint("tenant_id", "feature_key", name="uq_tenant_feature"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    feature_key = Column(String(128), nullable=False)
    enabled = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    updated_by = Column(String(255), nullable=True)


class MigrationStatus(str, enum.Enum):
    pending = "pending"
    applied = "applied"
    failed = "failed"


class MigrationLog(ControlBase):
    __tablename__ = "migration_log"
    __table_args__ = (UniqueConstraint("tenant_id", "migration_version", name="uq_tenant_migration"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    migration_version = Column(String(64), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    status = Column(Enum(MigrationStatus), nullable=False, default=MigrationStatus.pending)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
