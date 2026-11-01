"""Console auth for non-localhost use.

Localhost mode keeps the one time setup token (see web/app.py). When Pam
binds to a network address, a console password is required instead: the
password is hashed with PBKDF2 and stored in the data directory, logins
create session cookies, login attempts are rate limited per client, and
an optional PAM_ALLOWED_HOST value pins the public hostname.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

AUTH_FILENAME = "console_auth.json"
SESSION_COOKIE = "pam_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
MIN_PASSWORD_LENGTH = 8
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60.0
PBKDF2_ITERATIONS = 200_000

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_sessions: dict[str, float] = {}
_login_attempts: dict[str, list[float]] = {}


def data_dir(explicit: str | Path | None = None) -> Path:
    """Return the data dir, honoring an explicit path then PAM_HOME."""
    if explicit is not None:
        return Path(explicit).expanduser()
    home = os.environ.get("PAM_HOME", "").strip()
    if home:
        return Path(home).expanduser()
    try:
        from pam_pm.core.config import Config

        return Config().data_dir
    except Exception:  # noqa: BLE001
        return Path.cwd()


def auth_path(explicit: str | Path | None = None) -> Path:
    """Return the path of the stored password hash file."""
    if explicit is not None:
        return Path(explicit).expanduser() / AUTH_FILENAME
    return data_dir() / AUTH_FILENAME


def _read_record(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("salt") or not data.get("hash"):
        return None
    return data


def has_password(explicit: str | Path | None = None) -> bool:
    """Return True when a password hash is stored (never creates dirs)."""
    return _read_record(auth_path(explicit)) is not None


def _hash_password(password: str, salt: bytes, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return digest.hex()


def set_password(
    password: str, explicit: str | Path | None = None
) -> Path:
    """Hash and store a console password, returning the file path."""
    secret = (password or "").strip()
    if len(secret) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            "That password is too short. Use at least "
            f"{MIN_PASSWORD_LENGTH} characters."
        )
    salt = secrets.token_bytes(16)
    record = {
        "scheme": "pbkdf2-sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": salt.hex(),
        "hash": _hash_password(secret, salt, PBKDF2_ITERATIONS),
    }
    path = auth_path(explicit)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    logger.info("console password saved: path=%s", path)
    return path


def verify_password(
    password: str, explicit: str | Path | None = None
) -> bool:
    """Return True when the password matches the stored hash."""
    if not password:
        return False
    record = _read_record(auth_path(explicit))
    if record is None:
        return False
    try:
        salt = bytes.fromhex(str(record.get("salt", "")))
        iterations = int(record.get("iterations", PBKDF2_ITERATIONS))
    except (ValueError, TypeError):
        return False
    if iterations <= 0 or not salt:
        return False
    want = _hash_password(password.strip(), salt, iterations)
    return hmac.compare_digest(want, str(record.get("hash", "")))


def provision_password_from_env(
    explicit: str | Path | None = None,
) -> bool:
    """Store PAM_CONSOLE_PASSWORD on first run, returning True if stored.

    Does nothing when the variable is empty or a password is already
    stored. Raises ValueError with a plain message when the variable
    value is too short.
    """
    if has_password(explicit):
        return False
    secret = os.environ.get("PAM_CONSOLE_PASSWORD", "").strip()
    if not secret:
        return False
    if len(secret) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            "PAM_CONSOLE_PASSWORD is too short. "
            f"Use at least {MIN_PASSWORD_LENGTH} characters."
        )
    set_password(secret, explicit)
    return True


def normalize_host(host: str) -> str:
    """Return a lowercase hostname without port or brackets."""
    text = (host or "").strip().lower()
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text.strip("[]").strip()


def is_loopback_host(host: str) -> bool:
    """Return True for loopback bind addresses (localhost only)."""
    return normalize_host(host) in LOOPBACK_HOSTS


def console_bind_error(
    host: str, explicit: str | Path | None = None
) -> str | None:
    """Return a plain-English error when a bind must be refused.

    Returns None when the bind is allowed: localhost binds always are,
    and network binds are allowed once a console password is stored.
    """
    want = (host or "").strip() or "127.0.0.1"
    if is_loopback_host(want):
        return None
    if has_password(explicit):
        return None
    return (
        f"Cannot start Pam on {want} because no console password is set. "
        "Binding to a network address without a password would let anyone "
        "on the network open your console. Set a password first, then "
        "start again. Either run pam start on this machine without --host, "
        "open the login page, and choose a password, or set the "
        "PAM_CONSOLE_PASSWORD environment variable to at least "
        f"{MIN_PASSWORD_LENGTH} characters for first run. Localhost mode "
        "(127.0.0.1) keeps working with the one time setup token and needs "
        "no password."
    )


def create_session(ttl_seconds: float | None = None) -> str:
    """Create a session token valid for ttl_seconds from now."""
    token = secrets.token_urlsafe(32)
    ttl = SESSION_TTL_SECONDS if ttl_seconds is None else float(ttl_seconds)
    _sessions[token] = time.monotonic() + max(1.0, ttl)
    return token


def validate_session(token: str | None) -> bool:
    """Return True when a session token exists and has not expired."""
    if not token:
        return False
    expiry = _sessions.get(str(token))
    if expiry is None:
        return False
    if expiry < time.monotonic():
        _sessions.pop(str(token), None)
        return False
    return True


def destroy_session(token: str | None) -> None:
    """Drop one session token if present."""
    if token:
        _sessions.pop(str(token), None)


def reset_sessions() -> None:
    """Drop all sessions (tests use this)."""
    _sessions.clear()


def _prune_attempts(key: str, now: float) -> list[float]:
    cutoff = now - LOGIN_WINDOW_SECONDS
    kept = [moment for moment in _login_attempts.get(key, []) if moment > cutoff]
    if kept:
        _login_attempts[key] = kept
    else:
        _login_attempts.pop(key, None)
    return kept


def login_allowed(key: str) -> bool:
    """Return True when a client may attempt another login."""
    return len(_prune_attempts(key, time.monotonic())) < LOGIN_MAX_ATTEMPTS


def login_retry_after(key: str) -> int:
    """Return seconds until a rate limited client may retry."""
    attempts = _prune_attempts(key, time.monotonic())
    if len(attempts) < LOGIN_MAX_ATTEMPTS:
        return 0
    oldest = min(attempts)
    wait = LOGIN_WINDOW_SECONDS - (time.monotonic() - oldest)
    return max(1, int(wait) + 1)


def record_login_failure(key: str) -> None:
    """Record a failed login attempt for rate limiting."""
    now = time.monotonic()
    attempts = _prune_attempts(key, now)
    attempts.append(now)
    _login_attempts[key] = attempts


def record_login_success(key: str) -> None:
    """Clear a client's failed attempts after a good login."""
    _login_attempts.pop(key, None)


def reset_login_attempts() -> None:
    """Drop all recorded attempts (tests use this)."""
    _login_attempts.clear()


def parse_allowed_hosts(configured: str | None = None) -> list[str]:
    """Return the configured public hostnames (empty means any)."""
    raw = os.environ.get("PAM_ALLOWED_HOST", "") if configured is None else configured
    return [item for item in (normalize_host(part) for part in str(raw).split(",")) if item]


def host_allowed(host_header: str, configured: str | None = None) -> bool:
    """Return True when a Host header matches PAM_ALLOWED_HOST.

    Returns True for every host when nothing is configured.
    """
    allowed = parse_allowed_hosts(configured)
    if not allowed:
        return True
    return normalize_host(host_header) in allowed
