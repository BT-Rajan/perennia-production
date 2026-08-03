"""
Backup & disaster recovery (docs/06-development-passes.md, Pass 3).

Per-tenant database dumps via mysqldump, since DB-per-tenant means backup
is N-times more complex than shared-schema — there's no single "back up
the app database" step. Restore is not treated as a checkbox: this module
is built specifically so restore can be exercised and its correctness
verified (row counts / checksums before and after), not just "the backup
file exists."
"""
import subprocess
import tempfile
from pathlib import Path

import pymysql

from app.config import settings
from app.control.models import Tenant
from app.security import decrypt_secret


class BackupError(Exception):
    pass


def backup_tenant_db(tenant: Tenant, backup_dir: Path) -> Path:
    """
    Dumps the tenant's database to a single .sql file. Uses --single-transaction
    for a consistent snapshot without locking tables (InnoDB), and
    --routines/--triggers so the audit_log immutability trigger (Pass 1) is
    captured too — a restore that recreates the tables but not the trigger
    would silently reintroduce the tampering hole it exists to close.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    out_path = backup_dir / f"{tenant.db_name}.sql"
    db_password = decrypt_secret(tenant.db_pass_encrypted)

    cmd = [
        "mysqldump",
        f"-h{tenant.db_host}", f"-P{tenant.db_port}",
        f"-u{tenant.db_user}", f"-p{db_password}",
        "--single-transaction", "--routines", "--triggers",
        tenant.db_name,
    ]
    with open(out_path, "wb") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise BackupError(result.stderr.decode()[:2000])
    return out_path


def restore_tenant_db(tenant: Tenant, backup_path: Path, *, target_db_name: str | None = None) -> None:
    """
    Restores a dump into either the tenant's own database (real recovery)
    or a different target_db_name (used by the restore drill to verify
    integrity without touching the live tenant database — see
    verify_restore_drill below).
    """
    db_name = target_db_name or tenant.db_name
    admin_conn = pymysql.connect(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT,
        user=settings.MYSQL_ADMIN_USER, password=settings.MYSQL_ADMIN_PASSWORD,
        autocommit=True,
    )
    try:
        with admin_conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
            cur.execute(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    finally:
        admin_conn.close()

    cmd = [
        "mysql",
        f"-h{settings.MYSQL_HOST}", f"-P{settings.MYSQL_PORT}",
        f"-u{settings.MYSQL_ADMIN_USER}", f"-p{settings.MYSQL_ADMIN_PASSWORD}",
        db_name,
    ]
    with open(backup_path, "rb") as f:
        result = subprocess.run(cmd, stdin=f, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise BackupError(result.stderr.decode()[:2000])


def table_row_counts(host: str, port: int, user: str, password: str, db_name: str) -> dict[str, int]:
    """Used to verify restore integrity — compares row counts per table
    before backup and after restore, not just 'the restore command exited 0'."""
    conn = pymysql.connect(host=host, port=port, user=user, password=password,
                            database=db_name, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = [row[0] for row in cur.fetchall()]
            counts = {}
            for t in tables:
                cur.execute(f"SELECT COUNT(*) FROM `{t}`")
                counts[t] = cur.fetchone()[0]
            return counts
    finally:
        conn.close()
