#!/usr/bin/env python3
"""
Usage:
    python scripts/migrate.py --version 0001_app_config_notes
    python scripts/migrate.py --version 0001_app_config_notes --retry-failed
    python scripts/migrate.py --list

Applies a named migration (from app/core/migrations.py MIGRATIONS) across
every active/pending tenant, printing per-tenant status. No dashboard — see
docs/06-development-passes.md, Pass 1.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import get_control_session
from app.core.migrations import (
    MIGRATIONS, run_migration_across_tenants, failed_tenant_ids_for,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="Migration version to apply")
    parser.add_argument("--retry-failed", action="store_true",
                         help="Only re-run tenants currently marked failed for this version")
    parser.add_argument("--list", action="store_true", help="List known migrations")
    args = parser.parse_args()

    migrations_by_version = {v: stmts for v, stmts in MIGRATIONS}

    if args.list:
        for v, _ in MIGRATIONS:
            print(v)
        return

    if not args.version:
        parser.error("--version is required unless --list is given")
    if args.version not in migrations_by_version:
        print(f"Unknown migration version: {args.version}", file=sys.stderr)
        sys.exit(1)

    statements = migrations_by_version[args.version]

    with get_control_session() as control_session:
        only_ids = None
        if args.retry_failed:
            only_ids = failed_tenant_ids_for(control_session, args.version)
            if not only_ids:
                print("No failed tenants to retry for this version.")
                return
            print(f"Retrying {len(only_ids)} previously-failed tenant(s)...")

        results = run_migration_across_tenants(control_session, args.version, statements, only_tenant_ids=only_ids)

    applied = [r for r in results if r.status.value == "applied"]
    failed = [r for r in results if r.status.value == "failed"]

    for r in results:
        line = f"tenant_id={r.tenant_id} status={r.status.value}"
        if r.error:
            line += f" error={r.error[:200]}"
        print(line)

    print(f"\n{len(applied)}/{len(results)} applied, {len(failed)} failed.")
    if failed:
        print("Re-run with --retry-failed after investigating.")
        sys.exit(1)


if __name__ == "__main__":
    main()
