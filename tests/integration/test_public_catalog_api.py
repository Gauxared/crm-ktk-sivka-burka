"""Synthetic PostgreSQL contract checks for PUBLIC-002."""

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from sivka_burka_api.main import create_app
from sivka_burka_api.public_catalog import PublicCatalogRuntime


@pytest.fixture
def database():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is required")
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("TRUNCATE service_options, services, club_settings CASCADE"))
        c.execute(text("INSERT INTO club_settings (id, timezone, location_link, visit_rules) VALUES (1, 'Asia/Bangkok', 'https://example.invalid/location', 'Synthetic rules')"))
        sid, inactive = uuid4(), uuid4()
        c.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'synthetic', 'Synthetic ride', 'Public description', 'Public information', true, 1), (:id2, 'hidden', 'Hidden', 'PII hidden', 'secret', false, 2)"), {"id": sid, "id2": inactive})
        c.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:fixed, :sid, 'fixed', 60, 'FIXED_PER_PERSON', 12500, 'RUB', true), (:neg, :sid, 'negotiated', NULL, 'NEGOTIATED', NULL, 'RUB', true), (:hidden, :inactive, 'hidden', 1, 'FIXED_PER_PERSON', 1, 'RUB', true)"), {"fixed": uuid4(), "neg": uuid4(), "hidden": uuid4(), "sid": sid, "inactive": inactive})
    yield engine
    engine.dispose()


def test_catalog_exposes_active_data_only_and_does_not_write(database):
    with database.connect() as c:
        before = c.execute(text("SELECT count(*) FROM services")).scalar_one()
    response = TestClient(create_app(public_catalog_runtime=PublicCatalogRuntime(database))).get("/api/v1/public/catalog")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["location_link"].startswith("https://")
    assert {o["pricing_mode"] for o in data["services"][0]["options"]} == {"FIXED_PER_PERSON", "NEGOTIATED"}
    assert all("secret" not in str(data).lower() for _ in [0])
    assert all("hidden" not in str(data).lower() for _ in [0])
    with database.connect() as c:
        assert c.execute(text("SELECT count(*) FROM services")).scalar_one() == before
