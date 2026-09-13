"""Configuration parsing for the API scaffold."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


class SettingsError(ValueError):
    """Raised when environment settings are invalid for the selected runtime mode."""


@dataclass(frozen=True)
class Settings:
    environment: str
    database_url: str | None
    session_secret: str | None
    admin_bootstrap_login: str | None


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    values = os.environ if environ is None else environ
    environment = values.get("APP_ENV", "development").strip().lower()
    if environment not in {"development", "test", "production"}:
        raise SettingsError("APP_ENV must be development, test, or production")

    database_url = values.get("DATABASE_URL") or None
    session_secret = values.get("SESSION_SECRET") or None
    admin_bootstrap_login = values.get("ADMIN_BOOTSTRAP_LOGIN") or None
    if environment == "production":
        missing = [
            key
            for key, value in {
                "DATABASE_URL": database_url,
                "SESSION_SECRET": session_secret,
                "ADMIN_BOOTSTRAP_LOGIN": admin_bootstrap_login,
            }.items()
            if not value
        ]
        if missing:
            raise SettingsError("Missing required production settings: " + ", ".join(missing))

    return Settings(
        environment=environment,
        database_url=database_url,
        session_secret=session_secret,
        admin_bootstrap_login=admin_bootstrap_login,
    )
