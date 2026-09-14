from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from sivka_burka_api.admin_sessions import verify_password
from sivka_burka_api.provision_owner import ProvisioningError, provision_owner


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def disposable_database(monkeypatch):
    """Use only an explicitly supplied disposable PostgreSQL URL.

    No dotenv files and no default/source database URL are consulted.
    """
    configured = os.environ.get("SYNTHETIC_DATABASE_URL")
    if not configured:
        pytest.skip("SYNTHETIC_DATABASE_URL is required for disposable PostgreSQL tests")
    base = make_url(configured)
    name = "sivka_auth003_" + uuid4().hex[:12]
    target = base.set(database=name)
    admin_url = base.set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()
    monkeypatch.setenv("DATABASE_URL", target.render_as_string(hide_password=False))
    try:
        config = Config(str(ROOT / "alembic.ini"))
        command.upgrade(config, "head")
        engine = create_engine(target)
        try:
            yield engine
        finally:
            engine.dispose()
    finally:
        cleanup = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with cleanup.begin() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            cleanup.dispose()


def test_provision_owner_is_one_time_and_persists_only_hash(disposable_database):
    password = "synthetic-auth003-password"
    owner_id = provision_owner(
        os.environ["DATABASE_URL"], "synthetic-owner", password, password
    )
    with disposable_database.begin() as connection:
        owner = connection.execute(
            text("SELECT id, password_hash, credentials_version FROM owner_accounts")
        ).mappings().one()
    assert str(owner["id"]) == owner_id
    assert owner["credentials_version"] == 1
    assert owner["password_hash"] != password.encode()
    assert verify_password(owner["password_hash"], password)

    with pytest.raises(ProvisioningError, match="already exists"):
        provision_owner(os.environ["DATABASE_URL"], "second-owner", password, password)
    with disposable_database.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM owner_accounts")).scalar_one() == 1
