"""Server-side owner sessions; deployment policy is supplied by the runtime."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Protocol

from fastapi import Request
from sqlalchemy import Engine, text


_PASSWORD_PREFIX = "scrypt$v=1$n=16384$r=8$p=1"
_DUMMY_PASSWORD_HASH = b"scrypt$v=1$n=16384$r=8$p=1$salt=AAAAAAAAAAAAAAAAAAAAAA$digest=8CIxlGXRpVbk24CZvDx-Xn7x-wKeitbZlwnHtQ7o_q6qM_omzD6MXBI8Vl2zNCfsaDIDMx26qgJvuR_1wA_TtQ"
COOKIE_NAME = "admin_session"


class LoginAttemptLimiter(Protocol):
    def check(self, operation: str, request: Request) -> None: ...


class SessionLifetimePolicy(Protocol):
    def session_lifetime(self) -> timedelta: ...


class LoginRateLimited(Exception):
    """A runtime-provided login limiter rejected the request."""


def _b64encode(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise ValueError("invalid base64url")
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> bytes:
    """Create the accepted self-describing scrypt format for pre-provisioned records."""
    if not isinstance(password, str):
        raise ValueError("password must be text")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=64)
    return f"{_PASSWORD_PREFIX}$salt={_b64encode(salt)}$digest={_b64encode(digest)}".encode("ascii")


def verify_password(stored_hash: bytes, password: str) -> bool:
    """Verify only the versioned format and fail safely for malformed stored values."""
    try:
        parts = stored_hash.decode("ascii").split("$")
        if len(parts) != 7 or "$".join(parts[:5]) != _PASSWORD_PREFIX or not parts[5].startswith("salt=") or not parts[6].startswith("digest="):
            return False
        salt = _b64decode(parts[5].removeprefix("salt="))
        expected = _b64decode(parts[6].removeprefix("digest="))
        if len(salt) < 16 or len(expected) != 64:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=64)
        return hmac.compare_digest(actual, expected)
    except (AttributeError, UnicodeDecodeError, ValueError):
        return False


@dataclass(frozen=True)
class AdminSessionRuntime:
    engine: Engine
    token_hmac_secret: bytes
    login_attempt_limiter: LoginAttemptLimiter
    session_lifetime_policy: SessionLifetimePolicy
    trusted_origins: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.token_hmac_secret, bytes) or not self.token_hmac_secret:
            raise ValueError("token_hmac_secret must be supplied as non-empty runtime material")
        if not callable(getattr(self.login_attempt_limiter, "check", None)):
            raise ValueError("login_attempt_limiter must be a runtime dependency")
        if not callable(getattr(self.session_lifetime_policy, "session_lifetime", None)):
            raise ValueError("session_lifetime_policy must be a runtime dependency")
        if not self.trusted_origins or any(not isinstance(origin, str) or not origin for origin in self.trusted_origins):
            raise ValueError("trusted_origins must contain explicit origins")

    def lifetime(self) -> timedelta:
        lifetime = self.session_lifetime_policy.session_lifetime()
        if not isinstance(lifetime, timedelta) or lifetime <= timedelta():
            raise ValueError("session lifetime policy must return a positive timedelta")
        return lifetime

    def digest_token(self, token: str) -> bytes:
        try:
            return hmac.digest(self.token_hmac_secret, token.encode("ascii"), "sha256")
        except (AttributeError, UnicodeEncodeError):
            return b""

    def login(self, login: str, password: str) -> tuple[str, str, str, datetime] | None:
        with self.engine.connect() as connection:
            account = connection.execute(
                text("SELECT id, password_hash, disabled_at, credentials_version FROM owner_accounts WHERE login = :login"),
                {"login": login},
            ).mappings().one_or_none()
        candidate_hash = account["password_hash"] if account is not None else _DUMMY_PASSWORD_HASH
        password_valid = verify_password(candidate_hash, password)
        if account is None or account["disabled_at"] is not None or not password_valid:
            return None
        now = datetime.now(timezone.utc)
        expires_at = now + self.lifetime()
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        with self.engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO owner_sessions
                         (token_digest, owner_id, credentials_version, csrf_secret, created_at, last_seen_at, expires_at)
                       VALUES (:digest, :owner_id, :credentials_version, :csrf_secret, :created_at, :last_seen_at, :expires_at)"""),
                {"digest": self.digest_token(token), "owner_id": account["id"], "credentials_version": account["credentials_version"],
                 "csrf_secret": csrf_token.encode("ascii"), "created_at": now, "last_seen_at": now, "expires_at": expires_at},
            )
        return token, str(account["id"]), csrf_token, expires_at

    def active_session(self, token: str) -> dict[str, object] | None:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            session = connection.execute(
                text("""SELECT s.owner_id, s.credentials_version, s.csrf_secret, s.expires_at
                         FROM owner_sessions AS s JOIN owner_accounts AS a ON a.id = s.owner_id
                         WHERE s.token_digest = :digest AND s.revoked_at IS NULL AND s.expires_at > :now
                           AND a.disabled_at IS NULL AND a.credentials_version = s.credentials_version"""),
                {"digest": self.digest_token(token), "now": now},
            ).mappings().one_or_none()
            if session is not None:
                connection.execute(text("UPDATE owner_sessions SET last_seen_at = :now WHERE token_digest = :digest"), {"now": now, "digest": self.digest_token(token)})
        return dict(session) if session is not None else None

    def revoke(self, token: str, csrf_token: str) -> bool:
        session = self.active_session(token)
        try:
            supplied_csrf = csrf_token.encode("ascii")
        except (AttributeError, UnicodeEncodeError):
            return False
        if session is None or not hmac.compare_digest(session["csrf_secret"], supplied_csrf):
            return False
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            result = connection.execute(text("UPDATE owner_sessions SET revoked_at = :now WHERE token_digest = :digest AND revoked_at IS NULL"), {"now": now, "digest": self.digest_token(token)})
        return result.rowcount == 1
