"""Synthetic PostgreSQL contract checks for PUBLIC-002."""
from pathlib import Path
import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from sivka_burka_api.main import create_app
from sivka_burka_api.public_catalog import PublicCatalogRuntime

ROOT = Path(__file__).resolve().parents[2]

def synthetic_database_url() -> str:
    if configured := os.environ.get("DATABASE_URL"):
        return configured
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.removeprefix("DATABASE_URL=")
    raise RuntimeError("DATABASE_URL is not available for integration tests")

def disposable_url() -> tuple[URL, str]:
    source_url = make_url(synthetic_database_url())
    name = f"sivka_public_catalog_{uuid4().hex}"
    return source_url.set(database=name), name

@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch):
    target_url, name = disposable_url()
    source_url = make_url(synthetic_database_url())
    if target_url.database == source_url.database:
        raise AssertionError("disposable database must differ from configured source database")
    admin_url = target_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    database_engine = None
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        monkeypatch.setenv("DATABASE_URL", target_url.render_as_string(hide_password=False))
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        database_engine = create_engine(target_url)
        with database_engine.begin() as connection:
            connection.execute(text("INSERT INTO club_settings (id, timezone, location_link, visit_rules) VALUES (1, 'Asia/Bangkok', 'https://example.invalid/location', 'Synthetic rules')"))
            service_id, inactive_id = uuid4(), uuid4()
            connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'synthetic', 'Synthetic ride', 'Public description', 'Public information', true, 1), (:id2, 'hidden', 'Hidden', 'PII hidden', 'secret', false, 2)"), {"id": service_id, "id2": inactive_id})
            connection.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:fixed, :sid, 'fixed', 60, 'FIXED_PER_PERSON', 12500, 'RUB', true), (:neg, :sid, 'negotiated', NULL, 'NEGOTIATED', NULL, 'RUB', true), (:hidden, :inactive, 'hidden', 1, 'FIXED_PER_PERSON', 1, 'RUB', true)"), {"fixed": uuid4(), "neg": uuid4(), "hidden": uuid4(), "sid": service_id, "inactive": inactive_id})
        yield database_engine
    finally:
        if database_engine is not None:
            database_engine.dispose()
        admin_engine.dispose()
        cleanup_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with cleanup_engine.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            cleanup_engine.dispose()

def test_catalog_exposes_active_data_only_and_does_not_write(database):
    with database.connect() as connection:
        before = connection.execute(text("SELECT count(*) FROM services")).scalar_one()
    response = TestClient(create_app(public_catalog_runtime=PublicCatalogRuntime(database))).get("/api/v1/public/catalog")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["location_link"].startswith("https://")
    assert {option["pricing_mode"] for option in data["services"][0]["options"]} == {"FIXED_PER_PERSON", "NEGOTIATED"}
    assert "secret" not in str(data).lower()
    assert "hidden" not in str(data).lower()
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM services")).scalar_one() == before
