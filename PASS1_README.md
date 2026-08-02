# Pass 1 — Secure, Simple Foundation

Implements docs/06-development-passes.md Pass 1 (see BT-Rajan/perennia-platform-docs
for the full spec). Multi-tenant foundation: DB-per-tenant isolation, universal
audit logging, feature flags with entitlement gating, migration orchestration,
tenant-scoped admin login. No admin UI beyond login — provisioning and
migrations are CLI scripts by design (docs/08, "simplicity discipline").

## Setup

1. `pip install -r requirements.txt -r requirements-pass1.txt`
   (plus `pip install --break-system-packages` equivalents if not in a venv)
2. Have a MySQL 8.0+ server reachable. Create the control DB:
   `mysql -u root -p -e "CREATE DATABASE perennia_control"`
3. Copy `.env.example` to `.env` and fill in `SECRET_KEY`, `ENCRYPTION_KEY`,
   `ADMIN_PASSWORD_HASH`, `MYSQL_ADMIN_PASSWORD` (see app/security.py for how
   to generate the first three).
4. `python scripts/provision_tenant.py --name "Test Salon" --subdomain testsalon --plan growth --admin-username owner --admin-password <pw>`
5. `python -m pytest tests/test_pass1_foundation.py -v`

## What's real, what's not yet

Real and tested against actual MySQL (not mocked): tenant isolation
(MySQL-level, not just app-level — a tenant credential gets a genuine
Access Denied against another tenant's database), audit log immutability
(DB trigger, holds even against a connection with GRANT ALL), feature-flag
entitlement gating, migration runner with independent per-tenant retry,
tenant-scoped admin login with audit logging, basic webhook alerting.

Not yet done (Pass 2, per the redefined plan): any booking/customer-facing
functionality, the four tenant admin screens (Dashboard, Bookings, Staff &
Services, Knowledge Base), or the two platform admin screens (Tenants list,
Tenant detail). The existing single-tenant app/data/*.json-backed routes in
main.py are untouched and still function as before — Pass 1 is purely
additive, mounted under /api/tenant.

## Bugs found and fixed during implementation (worth knowing if extending this)

1. pymysql only does %-substitution when `args` is passed to `cursor.execute`
   — mixing that with MySQL's `'@'%'` host wildcard silently breaks unless
   scoped carefully (see app/control/provisioning.py comments).
2. MySQL privileges are additive across grant levels — a table-level REVOKE
   cannot narrow an already-granted DB-level GRANT ALL (error 1147, confirmed
   empirically). audit_log immutability is enforced via a BEFORE
   UPDATE/DELETE trigger instead, not a privilege grant.
3. Trigger creation requires SUPER (with binlog enabled), which a
   tenant-scoped credential correctly should not have — tables are created
   via the tenant credential, the trigger via the admin credential.
4. Standard MySQL (unlike MariaDB) does not support `IF NOT EXISTS` on
   `ADD COLUMN` — migration statements are plain DDL; idempotency comes from
   the per-tenant MigrationLog version tracking, not SQL syntax.
