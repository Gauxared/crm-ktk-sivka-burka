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
