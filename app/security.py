"""
Security primitives:
  - bcrypt password verification (admin login)
  - signed, expiring session tokens (itsdangerous) carried in an
    httpOnly cookie — the browser never sees or can read the token content
  - a CSRF token issued at login and required on every state-changing
    admin request, checked via a custom header (defeats simple cross-site
    form submission since it can't set custom headers cross-origin)
  - Fernet symmetric encryption for the LLM API key at rest, so a raw
    filesystem/backup leak of config.json does not hand over a usable key
"""
import hmac
import secrets

import bcrypt
from cryptography.fernet import Fernet
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="perennia-admin-session")
_fernet = Fernet(settings.ENCRYPTION_KEY.encode() if isinstance(settings.ENCRYPTION_KEY, str) else settings.ENCRYPTION_KEY)


def verify_password(plaintext: str, bcrypt_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), bcrypt_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_session_token(username: str, csrf_token: str) -> str:
    """Signed, timestamped token. Tampering or expiry both fail verification."""
    return _serializer.dumps({"u": username, "csrf": csrf_token})


def verify_session_token(token: str) -> dict | None:
    try:
        data = _serializer.loads(token, max_age=settings.SESSION_TTL_SECONDS)
        return data
    except (BadSignature, SignatureExpired):
        return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_tokens_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


def mask_key(plaintext_key: str) -> str:
    """Never send the real key back to the browser — only a display hint."""
    if not plaintext_key:
        return ""
    if len(plaintext_key) <= 8:
        return "•" * len(plaintext_key)
    return plaintext_key[:4] + "…" + plaintext_key[-4:]

