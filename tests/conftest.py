"""
Shared test setup. Control DB is reset exactly ONCE per test session, not
per module — resetting per module lets MySQL's AUTO_INCREMENT restart,
which can hand out a tenant ID already cached (by ID) in the process-global
tenant connection pool (app/core/db.py), pointing a new tenant at a
different, already-dropped tenant's stale connection. That can't happen in
production (tenant IDs are never reused there — the control DB is never
dropped), but it's a real trap for test fixtures. One reset per session
avoids it.
"""
import sys
from pathlib import Path

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.core.db import init_control_db


def _admin_conn():
    return pymysql.connect(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT,
                            user=settings.MYSQL_ADMIN_USER, password=settings.MYSQL_ADMIN_PASSWORD,
                            autocommit=True)


@pytest.fixture(scope="session", autouse=True)
def _setup_control_db_once():
    conn = _admin_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{settings.CONTROL_DB_NAME}`")
            cur.execute(f"CREATE DATABASE `{settings.CONTROL_DB_NAME}`")
    finally:
        conn.close()
    init_control_db()
    yield
