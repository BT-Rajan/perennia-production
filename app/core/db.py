"""
Database engine management.

Two distinct engine lifecycles:
  - control_engine: one, long-lived, for perennia_control.
  - tenant pool: many, created on demand, bounded and LRU-evicted so we
    don't hold one open connection pool per tenant forever as tenant count
    grows (docs/01-architecture.md, "Connection management at scale").

No code outside this module should construct a tenant SQLAlchemy engine
directly — always go through get_tenant_session() so eviction stays
centralized in one place.
"""
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.control.models import ControlBase, Tenant
from app.tenant.models import TenantBase

_control_engine = create_engine(
    f"mysql+pymysql://{settings.MYSQL_ADMIN_USER}:{settings.MYSQL_ADMIN_PASSWORD}"
    f"@{settings.MYSQL_HOST}:{settings.MYSQL_PORT}/{settings.CONTROL_DB_NAME}",
    pool_pre_ping=True,
)
ControlSession = sessionmaker(bind=_control_engine, expire_on_commit=False)


def init_control_db() -> None:
    """Create control DB tables if they don't exist. Idempotent."""
    ControlBase.metadata.create_all(_control_engine)


@contextmanager
def get_control_session():
    session = ControlSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class _TenantPool:
    """Bounded pool-of-pools, LRU eviction by idle time and by count cap."""

    def __init__(self, max_warm: int, idle_evict_seconds: int):
        self._max_warm = max_warm
        self._idle_evict_seconds = idle_evict_seconds
        self._lock = threading.Lock()
        # tenant_id -> (engine, sessionmaker, last_used_ts)
        self._entries: "OrderedDict[int, tuple]" = OrderedDict()

    def _evict_idle(self) -> None:
        now = time.time()
        stale = [
            tid for tid, (_, _, last_used) in self._entries.items()
            if now - last_used > self._idle_evict_seconds
        ]
        for tid in stale:
            engine, _, _ = self._entries.pop(tid)
            engine.dispose()

    def get_sessionmaker(self, tenant: Tenant, db_password: str):
        with self._lock:
            self._evict_idle()
            entry = self._entries.get(tenant.id)
            if entry is not None:
                engine, sm, _ = entry
                self._entries[tenant.id] = (engine, sm, time.time())
                self._entries.move_to_end(tenant.id)
                return sm

            if len(self._entries) >= self._max_warm:
                oldest_tid, (oldest_engine, _, _) = next(iter(self._entries.items()))
                self._entries.pop(oldest_tid)
                oldest_engine.dispose()

            url = (
                f"mysql+pymysql://{tenant.db_user}:{db_password}"
                f"@{tenant.db_host}:{tenant.db_port}/{tenant.db_name}"
            )
            engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=2)
            sm = sessionmaker(bind=engine, expire_on_commit=False)
            self._entries[tenant.id] = (engine, sm, time.time())
            return sm


_tenant_pool = _TenantPool(
    max_warm=settings.TENANT_POOL_MAX_WARM,
    idle_evict_seconds=settings.TENANT_POOL_IDLE_EVICT_SECONDS,
)


@contextmanager
def get_tenant_session(tenant: Tenant, db_password: str):
    """
    The ONLY way the rest of the app should get a tenant-scoped DB session.
    `db_password` is the already-decrypted tenant DB password — callers get
    it via app.security.decrypt_secret(tenant.db_pass_encrypted), never by
    reading db_pass_encrypted directly into a query or log line.
    """
    sm = _tenant_pool.get_sessionmaker(tenant, db_password)
    session = sm()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_tenant_db(db_host: str, db_port: int, db_name: str, db_user: str, db_password: str) -> None:
    """
    Create tenant DB tables (app_config, audit_log, admin_user) from the
    template, then add insert-only enforcement on audit_log.

    Two different credentials are used deliberately: tables are created via
    the tenant's own scoped credential (it has GRANT ALL on its own
    database, which is sufficient for CREATE TABLE). The trigger is created
    via the platform admin credential, because MySQL requires the SUPER
    privilege to create a trigger when binary logging is enabled — and a
    tenant-scoped credential correctly does NOT have SUPER (that's a global,
    not per-database, privilege; granting it would break isolation). This
    mirrors how provisioning already draws a line between what a tenant
    credential can do at runtime and what only the platform can do during
    setup.

    Note on the enforcement mechanism: MySQL privileges are additive across
    grant levels (a table-level REVOKE cannot override an already-granted
    DB-level GRANT ALL — confirmed empirically, error 1147). Maintaining a
    parallel per-table privilege allowlist as the schema grows across later
    passes would fight the simplicity principle (docs/08). A BEFORE
    UPDATE/DELETE trigger enforces insert-only at the storage engine level
    instead — it holds even for a connection with full GRANT ALL, and
    doesn't need updating when new tables are added in Pass 2+.
    """
    from sqlalchemy import text
    from app.config import settings as _settings

    tenant_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    tenant_engine = create_engine(tenant_url)
    try:
        TenantBase.metadata.create_all(tenant_engine)
    finally:
        tenant_engine.dispose()

    admin_url = (
        f"mysql+pymysql://{_settings.MYSQL_ADMIN_USER}:{_settings.MYSQL_ADMIN_PASSWORD}"
        f"@{db_host}:{db_port}/{db_name}"
    )
    admin_engine = create_engine(admin_url)
    try:
        with admin_engine.begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS trg_audit_log_no_update"))
            conn.execute(text("DROP TRIGGER IF EXISTS trg_audit_log_no_delete"))
            conn.execute(text("""
                CREATE TRIGGER trg_audit_log_no_update BEFORE UPDATE ON audit_log
                FOR EACH ROW
                BEGIN
                    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_log is insert-only';
                END
            """))
            conn.execute(text("""
                CREATE TRIGGER trg_audit_log_no_delete BEFORE DELETE ON audit_log
                FOR EACH ROW
                BEGIN
                    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_log is insert-only';
                END
            """))
    finally:
        admin_engine.dispose()
