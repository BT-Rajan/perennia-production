#!/usr/bin/env python3
"""
Starts the Perennia server.

- Plain HTTP on HOST:PORT (default 127.0.0.1:8001) by default.
- If SSL_CERTFILE and SSL_KEYFILE are both set in .env and point to files
  that actually exist, binds HTTPS directly on 0.0.0.0:HTTPS_PORT
  (default 443) instead. Binding to 443 normally requires
  Administrator (Windows) or root/sudo (macOS/Linux) privileges — if you
  hit a permission error, either run this with elevated privileges or
  put a reverse proxy (nginx, Caddy, etc.) in front of the plain-HTTP
  port instead.

Usage:
    python scripts/run_server.py
"""
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
from app.config import settings
from app.logging_config import uvicorn_log_config


def _open_browser_later(url: str, delay: float = 2.0) -> None:
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def main() -> None:
    cert = settings.SSL_CERTFILE
    key = settings.SSL_KEYFILE
    use_ssl = bool(cert and key and Path(cert).exists() and Path(key).exists())

    if (cert or key) and not use_ssl:
        print("[run] SSL_CERTFILE/SSL_KEYFILE set in .env but one or both files "
              "are missing — falling back to plain HTTP.")

    if use_ssl:
        url = f"https://localhost:{settings.HTTPS_PORT}"
        print(f"[run] Starting Perennia with HTTPS on 0.0.0.0:{settings.HTTPS_PORT}")
        print(f"[run] Admin panel: {url}/admin")
        _open_browser_later(url)
        uvicorn.run(
            "app.main:app",
            host="0.0.0.0",
            port=settings.HTTPS_PORT,
            ssl_certfile=cert,
            ssl_keyfile=key,
            log_config=uvicorn_log_config(),
        )
    else:
        url = f"http://{settings.HOST}:{settings.PORT}"
        print(f"[run] Starting Perennia on {url}")
        print(f"[run] Admin panel: {url}/admin")
        _open_browser_later(url)
        uvicorn.run(
            "app.main:app",
            host=settings.HOST,
            port=settings.PORT,
            log_config=uvicorn_log_config(),
        )


if __name__ == "__main__":
    main()
