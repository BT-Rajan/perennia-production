#!/usr/bin/env python3
"""
Usage:
    python scripts/provision_tenant.py --name "GlowSpa Kuwait" --subdomain glowspa --plan growth --admin-username sara --admin-password <pw>

Creates the tenant's isolated MySQL database, applies the schema, sets
default feature flags for the plan tier, and creates the first admin user.
No web UI — see docs/06-development-passes.md, Pass 1.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.control.models import PlanTier
from app.control.provisioning import provision_tenant, InvalidSubdomainError, SubdomainTakenError
from app.core.db import get_control_session, get_tenant_session, init_control_db
from app.core.feature_flags import set_feature_flag, PLAN_ENTITLEMENTS
from app.security import decrypt_secret, encrypt_secret
import bcrypt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="Business name")
    parser.add_argument("--subdomain", required=True)
    parser.add_argument("--plan", required=True, choices=[p.value for p in PlanTier])
    parser.add_argument("--admin-username", required=True)
    parser.add_argument("--admin-password", required=True)
    args = parser.parse_args()

    init_control_db()

    with get_control_session() as control_session:
        try:
            result = provision_tenant(
                control_session,
                business_name=args.name,
                subdomain=args.subdomain,
                plan_tier=PlanTier(args.plan),
            )
        except (InvalidSubdomainError, SubdomainTakenError) as e:
            print(f"FAILED: {e}", file=sys.stderr)
            sys.exit(1)

        if result.failed_step:
            print(f"PROVISIONING FAILED at step '{result.failed_step}': {result.error}", file=sys.stderr)
            print(f"Tenant row created (id={result.tenant.id}, status=provisioning_failed). "
                  f"Fix the underlying issue and re-run with --retry {result.tenant.id}.", file=sys.stderr)
            sys.exit(1)

        # Default feature flags for the plan tier.
        for feature_key in PLAN_ENTITLEMENTS[PlanTier(args.plan)]:
            set_feature_flag(
                control_session, tenant_id=result.tenant.id, plan_tier=PlanTier(args.plan),
                feature_key=feature_key, enabled=True, updated_by="provisioning-script",
            )

        db_password = decrypt_secret(result.tenant.db_pass_encrypted)
        tenant = result.tenant

    with get_tenant_session(tenant, db_password) as tenant_session:
        from app.tenant.models import AdminUser
        pw_hash = bcrypt.hashpw(args.admin_password.encode(), bcrypt.gensalt()).decode()
        tenant_session.add(AdminUser(username=args.admin_username, password_hash=pw_hash))

    print(f"OK: tenant '{args.subdomain}' provisioned (id={tenant.id}, db={tenant.db_name}, status=pending)")
    print(f"Admin login: username={args.admin_username}")


if __name__ == "__main__":
    main()
