# Perennia — AI assistant backend

FastAPI backend and static frontend for Perennia's website AI assistant:
a chat widget that answers visitor questions from an admin-managed
knowledge base, books/reschedules/cancels appointments (with Google
Calendar sync and email notifications), and an admin panel for managing
all of it.

## Security properties

This app is built so the LLM API key, admin credentials, and knowledge
base are never exposed to the browser:

| | |
|---|---|
| API key | Encrypted at rest (Fernet). Never sent to any browser, not even the admin's — only a masked hint like `sk-a…2345` is ever returned. The browser only ever talks to `/api/chat` on this server; the server holds the key and calls the LLM provider itself. |
| `config.json` / `knowledge_base.json` | Live in `data/`, which is never mounted as a static directory — there is no URL that serves them. |
| Admin auth | Real server-side session: bcrypt-hashed password, signed httpOnly cookie, checked on every admin request. Every state-changing admin route also requires a matching CSRF token header. |
| Image uploads | SVG rejected; every image is re-encoded through Pillow server-side, which strips anything hidden in the file regardless of what it claimed to be. |
| Document uploads (knowledge base) | Files are identified by sniffing actual bytes, not by trusting the extension or declared Content-Type. |
| Rate limiting | Per-IP limits on chat, login, and appointment booking. |
| Appointment retrieval | Requires the appointment ID *and* the email it was booked with — a guessed ID alone can't expose someone else's booking. |

## Features

- **Chat widget** — home page with a 7-second auto-transition into the
  chat, glass/navy UI, bilingual (EN/AR), FAQ quick-questions, and a
  rotating image/quote sidebar.
- **Talk to Us** — booking lives *inside* the chat shell (not a separate
  popup) and posts its result back into the conversation transcript:
  - **New Appointment** — pick a date/time from real availability
    (local bookings + optional Google Calendar free/busy), get a
    friendly appointment ID (`PRN-XXXXXXXX`).
  - **Manage Booking** — look up an existing appointment by ID + email,
    then reschedule or cancel it — allowed up until `APPT_MIN_NOTICE_HOURS`
    (default 6) before the appointment; blocked with a clear message
    after that.
  - Confirmation/reschedule/cancellation emails to both the visitor and
    the admin inbox (optional — degrades gracefully to "logged, not sent"
    if SMTP isn't configured).
- **Admin panel** — provider/model + API key, tone, knowledge base,
  contact/landing page copy, logo/avatar uploads, appointment list +
  cancel, daily interaction analytics.

## Project layout

```
app/            FastAPI backend
  main.py         Routes (chat, appointments, admin)
  config.py       Settings, loaded from .env
  storage.py      config.json / knowledge_base.json / appointments.json persistence
  scheduling.py   Slot availability + the 6-hour modify-notice rule
  gcal.py         Google Calendar sync (optional, no-op if unconfigured)
  email_util.py   SMTP sending (optional, no-op if unconfigured)
  notifications.py  Builds the booked/rescheduled/cancelled email content
  llm.py          LLM provider calls
  security.py     Sessions, CSRF, password hashing, API-key encryption
  extract.py      Knowledge-base document text extraction
  prompt.py       System prompt assembly
data/           config.json, knowledge_base.json, appointments.json — created at
                runtime, never web-served (see .gitignore)
public/         index.html, admin.html, static/images/ (served as-is)
scripts/
  setup_env.py    Generates .env with fresh secrets (idempotent)
  run_server.py   Starts uvicorn; binds HTTPS on 443 if SSL_CERTFILE/
                  SSL_KEYFILE are set, otherwise plain HTTP on PORT
  gen_secrets.py  Prints a SECRET_KEY / ENCRYPTION_KEY / ADMIN_PASSWORD_HASH
                  set (used internally by setup_env.py, or run directly to
                  rotate the admin password later)
install.bat / install.sh   One-time setup: venv, dependencies, .env
start.bat / start.sh       Run the server (opens the browser automatically)
INSTALL.md      Step-by-step setup guide
requirements.txt
.env.example
Dockerfile
```

## Local setup

See **[INSTALL.md](INSTALL.md)** for the full guide. Short version:

- **Windows:** `install.bat`, then `start.bat`
- **macOS / Linux:** `./install.sh`, then `./start.sh`

Your browser opens automatically to `http://localhost:8001`. Log into
`/admin` with the username/password saved to `ADMIN_CREDENTIALS.txt` by
the installer, then set your LLM provider + API key from *Provider &
Model* — that's the only place the key is ever entered.

`public/static/images/` ships empty — the logo/avatar files aren't part
of this repo (see `.gitignore`). The site shows a styled text wordmark
fallback until you upload real ones via *Admin → Logo Upload* /
*Admin → Assistant Persona*.

## Changing the admin password later

Re-run `python3 scripts/gen_secrets.py --password "new-password"` and
replace `ADMIN_PASSWORD_HASH` in `.env` (and `SECRET_KEY` too if you want
to force all existing sessions to log out).

Rotating `ENCRYPTION_KEY` makes any previously-saved API key
undecryptable — you'd need to re-enter it in the admin panel afterwards.

## Deploying for real

Two supported ways to serve HTTPS:

1. **Reverse proxy (recommended)** — nginx/Caddy/a load balancer
   terminates TLS and forwards to this app on plain HTTP (`PORT`,
   default 8001). See a sample nginx + Certbot config in `INSTALL.md`.
2. **Direct bind on 443** — set `SSL_CERTFILE` and `SSL_KEYFILE` in
   `.env`; `scripts/run_server.py` then binds HTTPS on `HTTPS_PORT`
   (default 443) instead of plain HTTP. Requires Administrator/root
   privileges to bind a port below 1024.

**With Docker:**
```bash
docker build -t perennia .
docker run -d \
  --env-file .env \
  -p 127.0.0.1:8001:8001 \
  -v $(pwd)/data:/srv/data \
  -v $(pwd)/public/static/images:/srv/public/static/images \
  --name perennia \
  perennia
```
Put the same nginx/Certbot TLS termination in front of it, or mount
real cert/key files and let it bind 443 directly.

## Operational notes

- **State lives in this instance's own MySQL database, not local
  files.** `DATABASE_URL`/`DB_TABLE_PREFIX` (both required, provisioned
  by SiteHub's paid-tier pipeline — see `.env.example`) point at a
  dedicated per-tenant database; back that up the normal MySQL way
  (`mysqldump`, a managed provider's snapshotting, etc.), not by
  copying a local directory.
- **No longer single-instance-only.** The pre-MySQL version of this app
  used local JSON files with atomic replacement, which was safe for one
  process but not for multiple instances editing the same files — that
  constraint is gone now that storage is MySQL, which handles concurrent
  writers correctly on its own. This app still runs as a single PM2
  process in practice (`ecosystem.config.js`), but that's now an
  operational choice for this deployment model, not a correctness
  requirement.
- **`/health`** returns `{"ok": true}` for uptime checks / load balancer
  health probes.
- Logs go to stdout — capture them with your process manager or
  container runtime as usual.
