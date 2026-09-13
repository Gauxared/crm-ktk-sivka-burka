"""HTTP-facing dependencies and commands for the public submission boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from fastapi import Request
from sqlalchemy import Engine, text

from .inquiries import _digest, generate_submission_token


class SubmissionRateLimitError(Exception):
    """A configured rate-limit adapter rejected this request."""


class SubmissionRateLimiter(Protocol):
    """External policy boundary; this package deliberately defines no quota policy."""

    def check(self, operation: str, request: Request) -> None: ...


@dataclass(frozen=True)
class PublicSubmissionRuntime:
    """Explicitly injected dependencies required before public writes are enabled."""

    engine: Engine
    hmac_secret: bytes
    rate_limiter: SubmissionRateLimiter
    digest_key_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.hmac_secret, bytes) or not self.hmac_secret:
            raise ValueError("hmac_secret must be supplied as non-empty runtime material")
        if not callable(getattr(self.rate_limiter, "check", None)):
            raise ValueError("rate_limiter must be supplied as a runtime dependency")
        if self.digest_key_version <= 0:
            raise ValueError("digest_key_version must be positive")

    def issue_token(self) -> tuple[str, datetime]:
        token = generate_submission_token()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        with self.engine.begin() as connection:
            connection.execute(text("SELECT id FROM business_write_guard WHERE id = 1 FOR UPDATE"))
            connection.execute(
                text("""INSERT INTO submission_tokens
                         (token_digest, digest_key_version, issued_at, expires_at)
                       VALUES (:digest, :version, :issued_at, :expires_at)"""),
                {
                    "digest": _digest(self.hmac_secret, token),
                    "version": self.digest_key_version,
                    "issued_at": datetime.now(timezone.utc),
                    "expires_at": expires_at,
                },
            )
        return token, expires_at
