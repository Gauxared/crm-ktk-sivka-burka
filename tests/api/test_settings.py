import pytest

from sivka_burka_api.settings import SettingsError, load_settings


def test_development_allows_no_database_url() -> None:
    settings = load_settings({"APP_ENV": "development"})

    assert settings.environment == "development"
    assert settings.database_url is None


def test_invalid_environment_is_rejected() -> None:
    with pytest.raises(SettingsError, match="APP_ENV"):
        load_settings({"APP_ENV": "demo"})


def test_production_requires_all_sensitive_values() -> None:
    with pytest.raises(SettingsError, match="DATABASE_URL, SESSION_SECRET, ADMIN_BOOTSTRAP_LOGIN"):
        load_settings({"APP_ENV": "production"})


def test_production_accepts_explicit_values() -> None:
    settings = load_settings(
        {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql+psycopg://test",
            "SESSION_SECRET": "test-secret",
            "ADMIN_BOOTSTRAP_LOGIN": "owner",
        }
    )

    assert settings.environment == "production"


def test_telegram_is_disabled_by_default_and_enabled_settings_are_redacted() -> None:
    assert load_settings({"APP_ENV": "test"}).telegram.enabled is False
    settings = load_settings({
        "APP_ENV": "test", "TELEGRAM_ENABLED": "true",
        "TELEGRAM_INTEGRATION_ID": "telegram-primary", "TELEGRAM_WEBHOOK_SECRET": "synthetic_secret",
        "TELEGRAM_BOT_TOKEN": "1234567890:synthetic_token", "TELEGRAM_CHANNEL_HMAC_SECRET": "synthetic-hmac",
        "TELEGRAM_DIGEST_KEY_VERSION": "1",
    })
    rendered = repr(settings.telegram)
    assert settings.telegram.enabled is True
    assert "synthetic_secret" not in rendered and "synthetic_token" not in rendered and "synthetic-hmac" not in rendered


@pytest.mark.parametrize("override", [
    {"TELEGRAM_ENABLED": "yes"},
    {"TELEGRAM_ENABLED": "true", "TELEGRAM_INTEGRATION_ID": "telegram-primary"},
    {"TELEGRAM_ENABLED": "true", "TELEGRAM_INTEGRATION_ID": "telegram-primary", "TELEGRAM_WEBHOOK_SECRET": "bad secret", "TELEGRAM_BOT_TOKEN": "1234567890:synthetic_token", "TELEGRAM_CHANNEL_HMAC_SECRET": "synthetic-hmac", "TELEGRAM_DIGEST_KEY_VERSION": "1"},
    {"TELEGRAM_ENABLED": "true", "TELEGRAM_INTEGRATION_ID": "telegram-primary", "TELEGRAM_WEBHOOK_SECRET": "synthetic_secret", "TELEGRAM_BOT_TOKEN": "1234567890:synthetic_token", "TELEGRAM_CHANNEL_HMAC_SECRET": "synthetic-hmac", "TELEGRAM_DIGEST_KEY_VERSION": "0"},
])
def test_invalid_telegram_settings_fail_closed_without_values(override: dict[str, str]) -> None:
    secret = "do-not-disclose"
    values = {"APP_ENV": "test", **override, "TELEGRAM_CHANNEL_HMAC_SECRET": override.get("TELEGRAM_CHANNEL_HMAC_SECRET", secret)}
    with pytest.raises(SettingsError) as raised:
        load_settings(values)
    assert "do-not-disclose" not in str(raised.value)
