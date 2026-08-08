"""
Perennia's storage tests run against a REAL MySQL database
(sitehub_tenant_tst1, a standalone tenant DB provisioned for this test
suite — not a mock, not sqlite) using a real Fernet key for the API-key
encryption round-trip.

Environment is set before any test module imports app.config (which
loads it once at import time via python-dotenv, default override=False —
an already-set env var wins over .env), so the whole test session
targets the test tenant database rather than whatever's in .env.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DATABASE_URL", "mysql+pymysql://tenant_tst1:devonlytenanttestpass123@localhost/sitehub_tenant_tst1")
os.environ.setdefault("DB_TABLE_PREFIX", "tst1")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-use-only")
os.environ.setdefault("ENCRYPTION_KEY", "VXfu9x8EJUkzxQVxu7aHoEeQgm4AjFXxJIMZUSkpFN8=")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$2b$12$UAQuLJ0vxTehlzpDPgWoRe4zHIYu.vjz4p0mFTzSG3c/43EFbzlFq")
os.environ.setdefault("COOKIE_SECURE", "false")

import pytest
from sqlalchemy import text

from app import storage


@pytest.fixture(autouse=True)
def _clean_tables():
    """Truncates every one of this tenant's own tables before each test,
    so tests don't leak state into each other despite sharing one real,
    already-migrated (ensure_tables() ran at import time) database."""
    with storage._engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for table in (storage.CONFIG_TABLE, storage.KB_TABLE, storage.APPT_TABLE,
                      storage.LEADS_TABLE, storage.STATS_TABLE):
            conn.execute(text(f"DELETE FROM `{table}`"))
    yield
