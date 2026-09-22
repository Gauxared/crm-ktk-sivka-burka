"""Configuration parsing for the API scaffold."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Mapping


class SettingsError(ValueError):
    """Raised when environment settings are invalid for the selected runtime mode."""


@dataclass(frozen=True)
class TelegramRuntimeSettings:
    enabled: bool
    integration_id: str | None = None
    webhook_secret: str | None = None
    bot_token: str | None = None
    channel_hmac_secret: bytes | None = None
    digest_key_version: int | None = None

    def __repr__(self) -> str:
        return (
            "TelegramRuntimeSettings("
            f"enabled={self.enabled!r}, integration_id={self.integration_id!r}, "
            "webhook_secret=<redacted>, bot_token=<redacted>, "
            "channel_hmac_secret=<redacted>, "
            f"digest_key_version={self.digest_key_version!r})"
        )


@dataclass(frozen=True)
class Settings:
    environment: str
    database_url: str | None
    session_secret: str | None
    admin_bootstrap_login: str | None
    telegram: TelegramRuntimeSettings


def _telegram_error(code: str) -> SettingsError:
    return SettingsError(f"TELEGRAM_CONFIGURATION_INVALID: {code}")


def _telegram_settings(values: Mapping[str, str]) -> TelegramRuntimeSettings:
    enabled_value = values.get("TELEGRAM_ENABLED", "false")
    if enabled_value not in {"true", "false"}:
        raise _telegram_error("TELEGRAM_ENABLED")
    if enabled_value == "false":
        return TelegramRuntimeSettings(enabled=False)

    required = {
        "TELEGRAM_INTEGRATION_ID": values.get("TELEGRAM_INTEGRATION_ID"),
        "TELEGRAM_WEBHOOK_SECRET": values.get("TELEGRAM_WEBHOOK_SECRET"),
        "TELEGRAM_BOT_TOKEN": values.get("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_CHANNEL_HMAC_SECRET": values.get("TELEGRAM_CHANNEL_HMAC_SECRET"),
        "TELEGRAM_DIGEST_KEY_VERSION": values.get("TELEGRAM_DIGEST_KEY_VERSION"),
    }
    missing = [name for name, value in required.items() if not isinstance(value, str) or not value]
    if missing:
        raise _telegram_error("MISSING_" + "_".join(missing))
    integration_id = required["TELEGRAM_INTEGRATION_ID"]
    webhook_secret = required["TELEGRAM_WEBHOOK_SECRET"]
    bot_token = required["TELEGRAM_BOT_TOKEN"]
    hmac_secret = required["TELEGRAM_CHANNEL_HMAC_SECRET"]
    digest_value = required["TELEGRAM_DIGEST_KEY_VERSION"]
    assert all(isinstance(value, str) for value in (integration_id, webhook_secret, bot_token, hmac_secret, digest_value))
    if integration_id != integration_id.strip() or not 1 <= len(integration_id) <= 128 or "\x00" in integration_id:
        raise _telegram_error("TELEGRAM_INTEGRATION_ID")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", webhook_secret) is None:
        raise _telegram_error("TELEGRAM_WEBHOOK_SECRET")
    token_prefix, separator, token_suffix = bot_token.partition(":")
    if (
        separator != ":" or not 10 <= len(bot_token) <= 128 or not token_prefix.isdigit()
        or not token_suffix.isascii() or not token_suffix.replace("-", "").replace("_", "").isalnum()
    ):
        raise _telegram_error("TELEGRAM_BOT_TOKEN")
    if hmac_secret != hmac_secret.strip() or not hmac_secret or "\x00" in hmac_secret:
        raise _telegram_error("TELEGRAM_CHANNEL_HMAC_SECRET")
    try:
        digest_key_version = int(digest_value)
    except ValueError:
        raise _telegram_error("TELEGRAM_DIGEST_KEY_VERSION") from None
    if str(digest_key_version) != digest_value or digest_key_version < 1:
        raise _telegram_error("TELEGRAM_DIGEST_KEY_VERSION")
    return TelegramRuntimeSettings(True, integration_id, webhook_secret, bot_token, hmac_secret.encode("utf-8"), digest_key_version)


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    values = os.environ if environ is None else environ
    environment = values.get("APP_ENV", "development").strip().lower()
    if environment not in {"development", "test", "production"}:
        raise SettingsError("APP_ENV must be development, test, or production")

    database_url = values.get("DATABASE_URL") or None
    session_secret = values.get("SESSION_SECRET") or None
    admin_bootstrap_login = values.get("ADMIN_BOOTSTRAP_LOGIN") or None
    telegram = _telegram_settings(values)
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
        telegram=telegram,
    )
