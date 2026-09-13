from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[2]


def source_url():
    value = os.environ.get("DATABASE_URL")
    if not value:
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                value = line.split("=", 1)[1]
                break
    if not value:
        raise RuntimeError("DATABASE_URL is not available")
    return make_url(value)


@pytest.fixture()
def database(monkeypatch):
    name = "sivka_db006_" + uuid4().hex[:12]
    target = source_url().set(database=name)
    admin_url = target.set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.begin() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    monkeypatch.setenv("DATABASE_URL", target.render_as_string(hide_password=False))
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        engine = create_engine(target)
        try:
            yield engine
        finally:
            engine.dispose()
    finally:
        cleanup = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with cleanup.begin() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        cleanup.dispose()


def account_values():
    return {"id": uuid4(), "login": "owner", "password_hash": b"hash", "credentials_version": 1}


def test_head_contains_owner_storage_and_constraints(database):
    with database.begin() as connection:
        tables = connection.execute(text("SELECT table_name FROM information_schema.tables WHERE table_name IN ('owner_accounts','owner_sessions')")).scalars().all()
        assert set(tables) == {"owner_accounts", "owner_sessions"}
        connection.execute(text("INSERT INTO owner_accounts (id, login, password_hash, credentials_version) VALUES (:id, :login, :password_hash, :credentials_version)"), account_values())
        now = datetime.now(timezone.utc)
        connection.execute(text("INSERT INTO owner_sessions (token_digest, owner_id, credentials_version, csrf_secret, created_at, last_seen_at, expires_at) VALUES (:token, :owner, 1, :csrf, :created, :seen, :expires)"), {"token": b"digest", "owner": connection.execute(text("SELECT id FROM owner_accounts")).scalar_one(), "csrf": b"csrf", "created": now, "seen": now, "expires": now + timedelta(hours=1)})


@pytest.mark.parametrize("sql,params", [
    ("INSERT INTO owner_accounts (id, login, password_hash, credentials_version) VALUES (:id, 'owner', :hash, 1)", {"id": uuid4(), "hash": b"hash"}),
    ("INSERT INTO owner_accounts (id, login, password_hash, credentials_version) VALUES (:id, 'other', :hash, 0)", {"id": uuid4(), "hash": b"hash"}),
    ("INSERT INTO owner_accounts (id, login, password_hash, credentials_version) VALUES (:id, 'empty', :hash, 1)", {"id": uuid4(), "hash": b""}),
])
def test_account_constraints(database, sql, params):
    with database.begin() as connection:
        connection.execute(text("INSERT INTO owner_accounts (id, login, password_hash, credentials_version) VALUES (:id, 'owner', :hash, 1)"), {"id": uuid4(), "hash": b"hash"})
    with pytest.raises(IntegrityError):
        with database.begin() as connection:
            connection.execute(text(sql), params)


@pytest.mark.parametrize("fields", [
    {"owner": uuid4(), "digest": b"bad-owner", "csrf": b"csrf", "created": datetime(2026, 1, 1, tzinfo=timezone.utc), "seen": datetime(2026, 1, 1, tzinfo=timezone.utc), "expires": datetime(2025, 1, 1, tzinfo=timezone.utc)},
    {"owner": None, "digest": b"no-owner", "csrf": b"csrf", "created": datetime(2026, 1, 1, tzinfo=timezone.utc), "seen": datetime(2026, 1, 1, tzinfo=timezone.utc), "expires": datetime(2027, 1, 1, tzinfo=timezone.utc)},
    {"owner": None, "digest": b"no-csrf", "csrf": b"", "created": datetime(2026, 1, 1, tzinfo=timezone.utc), "seen": datetime(2026, 1, 1, tzinfo=timezone.utc), "expires": datetime(2027, 1, 1, tzinfo=timezone.utc)},
])
def test_session_constraints(database, fields):
    with database.begin() as connection:
        owner = account_values()
        connection.execute(text("INSERT INTO owner_accounts (id, login, password_hash, credentials_version) VALUES (:id, :login, :password_hash, :credentials_version)"), owner)
        if fields["owner"] is None and fields["digest"] == b"no-csrf":
            fields = {**fields, "owner": owner["id"]}
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO owner_sessions (token_digest, owner_id, credentials_version, csrf_secret, created_at, last_seen_at, expires_at) VALUES (:digest, :owner, 1, :csrf, :created, :seen, :expires)"), fields)
