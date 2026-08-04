# Installing Perennia

Requires Python 3.10+ (download from https://www.python.org/downloads/ —
on Windows, check "Add Python to PATH" during setup).

## 1. Install

- **Windows:** double-click `install.bat`
- **macOS / Linux:** open a terminal in this folder and run `./install.sh`

This creates a virtual environment, installs dependencies, and generates
the secrets the app needs (`.env`). It also creates `ADMIN_CREDENTIALS.txt`
with your admin login — save that password somewhere safe, then delete
the file.

## 2. Start

- **Windows:** double-click `start.bat`
- **macOS / Linux:** run `./start.sh`

Your browser opens automatically to **http://localhost:8001**.
Leave the terminal/console window open — closing it stops the server.

## 3. Add your API key

Log into the admin panel at **http://localhost:8001/admin** with the
username/password from `ADMIN_CREDENTIALS.txt`, then paste your Anthropic
API key into the settings there. It's encrypted before it's saved — you
never need to edit a config file by hand.

## Going live on port 443 (HTTPS)

By default Perennia runs on plain HTTP at port 8001, which is fine for
local use or behind a reverse proxy (nginx, Caddy, a load balancer, etc.)
that terminates HTTPS for you — that's the recommended setup for a public
deployment.

If you'd rather have Perennia bind directly to port 443 itself (no
reverse proxy), get a certificate and key file (e.g. from Let's Encrypt),
then add these lines to `.env`:

```
HTTPS_PORT=443
SSL_CERTFILE=/path/to/fullchain.pem
SSL_KEYFILE=/path/to/privkey.pem
```

Restart with `start.bat` / `start.sh`. Binding to port 443 requires
Administrator privileges (Windows) or root/sudo (macOS/Linux) — if the
server fails to start with a permissions error, either run it elevated or
use a reverse proxy instead. When both files are present, the app serves
**https://yourdomain/** on 443; when they're absent, it falls back to
plain HTTP on 8001 automatically.

## Appointment emails

The "Talk to Us" booking flow (inside the chat) works locally without any
extra setup — but to send confirmation, reschedule, and cancellation
emails to both the visitor and your team, add SMTP details to `.env`:

```
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=your-smtp-username
SMTP_PASSWORD=your-smtp-password
SMTP_FROM=Perennia <no-reply@perennia.com>
ADMIN_NOTIFY_EMAIL=info@perennia.com
```

Restart the server after editing `.env`. If `ADMIN_NOTIFY_EMAIL` is left
out, admin notifications go to the contact email set in the admin panel
instead. Without SMTP configured, bookings still work — emails are just
skipped (logged as a warning), never blocking the appointment itself.

Visitors can reschedule or cancel their own appointment from the same
chat panel, using their appointment ID plus the email they booked with,
up until 6 hours before the appointment (configurable via
`APPT_MIN_NOTICE_HOURS` in `.env`).

## Troubleshooting

**"Refused to connect" in the browser**
The server isn't running (or crashed on startup). Check the terminal/console
window it was started from for an error message. The most common cause is
a missing or incomplete `.env` file — re-run the installer, which will
regenerate it.

**"Python not found"**
Python isn't installed, or wasn't added to PATH. Reinstall Python and make
sure the "Add Python to PATH" option is checked (Windows), then run the
installer again.

**Port 8001 already in use**
Another program is using that port. Close it, or edit `PORT=8001` in `.env`
to a different number and restart.

## Uninstalling

Delete this folder. Everything (the virtual environment, data, and config)
lives inside it.
