# Perennia — production build

This is a from-scratch backend rewrite of the original prototype, keeping
the same visual design and admin features but fixing the architecture so
the LLM API key, admin credentials, and knowledge base are never exposed
to the browser.

## What changed vs. the prototype (short version)

| Prototype | This build |
|---|---|
| Browser fetched `config.json` directly and called the LLM provider itself, with the raw API key in a client-side header | Browser only ever talks to `/api/chat` on our own server; the server holds the key and calls the provider |
| `config.json` / `knowledge_base.json` sat in the static web root — readable by any visitor | Both live in `data/`, which is never mounted as a static directory; there is no URL that serves them |
| Admin "login" was a hardcoded password checked in client-side JS, with the password visible in the page source | Real server-side session: bcrypt-hashed password, signed httpOnly cookie, checked on every admin request |
| Every `/admin/*` endpoint had no authentication at all — anyone could POST to it directly | Every admin route requires a valid session, and every state-changing one also requires a matching CSRF token header |
| API key stored in plaintext on disk | API key encrypted at rest (Fernet); never re-sent to any browser, not even the admin's — only a masked hint like `sk-a…2345` |
| Logo/avatar uploads accepted raw SVG (possible stored XSS) and trusted the declared Content-Type | SVG rejected; every image is re-encoded through Pillow server-side, which also strips anything hidden in the file regardless of what it claimed to be |
| Knowledge-base document uploads trusted file extension | Files are identified by sniffing actual bytes; a renamed non-PDF claiming to be a PDF is rejected |
| No rate limiting anywhere | Per-IP rate limits on the chat endpoint and on login attempts |
| Admin panel settings (tone, contact info, "saved" API config) were stored in `localStorage` — which only ever affected the admin's own browser, not the live site | Everything is server-side config that immediately affects what real visitors see |
| Dead/broken CORS-preflight code inside the logo upload handler | Removed; CORS is explicit and off by default (same-origin doesn't need it) |

## Project layout

```
app/            FastAPI backend
data/           config.json + knowledge_base.json (created at runtime, never web-served)
public/         index.html, admin.html, static/images/ (served as-is)
scripts/        gen_secrets.py — generates the values .env needs
requirements.txt
.env.example
Dockerfile
```

## Local setup

0. **Note on images:** `public/static/images/` ships empty — the original
   uploads didn't include Perennia's actual logo/avatar files. The site
   will show broken image icons for the logo until you upload one via
   *Admin → Logo Upload* (and optionally *Admin → Assistant Persona* for
   the chat avatar). Everything else works fine before that.

1. **Install dependencies** (Python 3.11+):
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Generate secrets and an admin password:**
   ```bash
   python3 scripts/gen_secrets.py
   ```
   Copy the three printed lines into a new `.env` file (copy `.env.example` first).
   For local (non-HTTPS) testing, also set `COOKIE_SECURE=false` in `.env` —
   browsers silently drop `Secure` cookies over plain `http://`.

3. **Run it:**
   ```bash
   uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```
   Visit `http://127.0.0.1:8000/` for the site and `http://127.0.0.1:8000/admin`
   for the admin panel. Log in with the username in `.env` (`admin` by default)
   and the password you chose in step 2.

4. **Set up the assistant:** in the admin panel, go to *Provider & Model*,
   choose a provider, paste in a real API key, and save. That key is encrypted
   and written to `data/config.json` — it never touches your browser again
   after this point.

## Changing the admin password later

Re-run `python3 scripts/gen_secrets.py --password "new-password"` and replace
`ADMIN_PASSWORD_HASH` (and `SECRET_KEY`/`ENCRYPTION_KEY` if you want to force
all sessions to log out — rotating `SECRET_KEY` alone does that).

Rotating `ENCRYPTION_KEY` will make any previously-saved API key
undecryptable — you'd need to re-enter it in the admin panel afterwards.

## Deploying for real

This app is designed to sit behind a reverse proxy that terminates TLS —
it does not do HTTPS itself.

**Minimal systemd + nginx setup:**

1. Copy the project to the server, create `.env` there (never commit it),
   set `COOKIE_SECURE=true`.
2. Run the app with a process manager, e.g. a systemd unit:
   ```ini
   [Unit]
   Description=Perennia backend
   After=network.target

   [Service]
   WorkingDirectory=/srv/perennia
   ExecStart=/srv/perennia/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
   Restart=always
   User=perennia

   [Install]
   WantedBy=multi-user.target
   ```
3. Reverse-proxy it with nginx and get a certificate via Certbot:
   ```nginx
   server {
       listen 443 ssl;
       server_name your-domain.com;
       ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
       ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

       location / {
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   server {
       listen 80;
       server_name your-domain.com;
       return 301 https://$host$request_uri;
   }
   ```
   ```bash
   sudo certbot --nginx -d your-domain.com
   ```

**Or with Docker:**
```bash
docker build -t perennia .
docker run -d \
  --env-file .env \
  -p 127.0.0.1:8000:8000 \
  -v $(pwd)/data:/srv/data \
  -v $(pwd)/public/static/images:/srv/public/static/images \
  --name perennia \
  perennia
```
Put the same nginx/Certbot TLS termination in front of it either way.

## Operational notes

- **Back up `data/`.** It's the only state the app has (config + knowledge
  base). It's plain JSON, so `cp -r data/ backup/` is a complete backup.
- **Single-instance only, as written.** Config/knowledge-base writes use
  atomic file replacement, which is safe for one process but not for
  multiple app instances behind a load balancer editing the same files.
  If you outgrow a single instance, move `storage.py` onto a real database
  (Postgres/SQLite) — everything else calls through that one module, so
  it's a contained change.
- **`/health`** returns `{"ok": true}` for uptime checks / load balancer
  health probes.
- Logs go to stdout — capture them with your process manager or container
  runtime as usual.
