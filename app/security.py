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
  - an in-memory session revocation list, so logout invalidates a token
    immediately instead of leaving it valid (replayable) until it expires
"""
import hmac
import secrets
import threading

import bcrypt
from cryptography.fernet import Fernet
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="perennia-admin-session")
_fernet = Fernet(settings.ENCRYPTION_KEY.encode() if isinstance(settings.ENCRYPTION_KEY, str) else settings.ENCRYPTION_KEY)

# Tokens revoked before their natural expiry (logout, incident response).
# Process-local and in-memory: fine for the single-instance deployment this
# app targets (see the instance lock in main.py); a multi-instance
# deployment would need a shared store (e.g. Redis) instead.
_revoked_sessions: set[str] = set()
_revoked_lock = threading.Lock()

# Bumped by revoke_all_sessions() to invalidate every session issued
# before the bump, without having to enumerate or store every live
# token. Every new token is stamped with the epoch current at issue
# time; verification rejects any token stamped with an older epoch.
_revocation_epoch = 0


def verify_password(plaintext: str, bcrypt_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), bcrypt_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_session_token(username: str, csrf_token: str) -> str:
    """Signed, timestamped token. Tampering or expiry both fail verification."""
    with _revoked_lock:
        epoch = _revocation_epoch
    return _serializer.dumps({"u": username, "csrf": csrf_token, "epoch": epoch})


def verify_session_token(token: str) -> dict | None:
    try:
        data = _serializer.loads(token, max_age=settings.SESSION_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    with _revoked_lock:
        if token in _revoked_sessions:
            return None
        if data.get("epoch", -1) < _revocation_epoch:
            return None
    return data


def revoke_session_token(token: str) -> None:
    """Immediately invalidate one session token (logout)."""
    if token:
        with _revoked_lock:
            _revoked_sessions.add(token)


def revoke_all_sessions() -> None:
    """Invalidate every currently-issued session at once (security
    incident response) by bumping the revocation epoch, rather than
    trying to enumerate live tokens. For a compromised SECRET_KEY, also
    rotate SECRET_KEY — this alone doesn't stop an attacker who already
    knows the old key from forging new tokens signed with it."""
    global _revocation_epoch
    with _revoked_lock:
        _revocation_epoch += 1
        _revoked_sessions.clear()  # superseded by the epoch bump


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

