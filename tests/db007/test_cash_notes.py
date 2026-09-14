from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DatabaseError, IntegrityError

ROOT = Path(__file__).resolve().parents[2]


def configured_source_url() -> URL:
    value = os.environ.get("DATABASE_URL")
    if not value:
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                value = line.split("=", 1)[1]
                break
    if not value:
        raise RuntimeError("DATABASE_URL is required for disposable PostgreSQL tests")
    return make_url(value)


@pytest.fixture()
def database(monkeypatch):
    source = configured_source_url()
    name = "sivka_db007_" + uuid4().hex[:12]
    target = source.set(database=name)
    if target.render_as_string(hide_password=False) == source.render_as_string(hide_password=False):
        raise RuntimeError("disposable database must not equal DATABASE_URL")
    admin_url = source.set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
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
        try:
            with cleanup.begin() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            cleanup.dispose()


def owner_and_inquiry(connection):
    owner_id, contact_id, inquiry_id = uuid4(), uuid4(), uuid4()
    connection.execute(
        text("INSERT INTO owner_accounts (id, login, password_hash, credentials_version) VALUES (:id, :login, :hash, 1)"),
        {"id": owner_id, "login": "owner-" + uuid4().hex, "hash": b"synthetic"},
    )
    connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
    connection.execute(
        text("""INSERT INTO inquiries
                (id, contact_id, source_kind, status, contact_snapshot, selection_snapshot,
                 requester_name, contact_kind, contact_value)
                VALUES (:id, :contact_id, 'PHONE', 'NEW', '{}'::jsonb, '{}'::jsonb,
                        'Synthetic', 'PHONE', '+70000000000')"""),
        {"id": inquiry_id, "contact_id": contact_id},
    )
    return owner_id, inquiry_id


def note_values(owner_id, inquiry_id, **overrides):
    values = {
        "id": uuid4(),
        "inquiry_id": inquiry_id,
        "kind": "RECEIPT",
        "amount_minor": 100,
        "currency": "RUB",
        "occurred_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "owner_id": owner_id,
        "note": "synthetic cash note",
    }
    values.update(overrides)
    return values


INSERT_NOTE = text("""INSERT INTO cash_notes
    (id, inquiry_id, kind, amount_minor, currency, occurred_at, owner_id, note)
    VALUES (:id, :inquiry_id, :kind, :amount_minor, :currency, :occurred_at, :owner_id, :note)""")


def test_head_contains_append_only_cash_notes(database):
    with database.begin() as connection:
        owner_id, inquiry_id = owner_and_inquiry(connection)
        note = note_values(owner_id, inquiry_id)
        connection.execute(INSERT_NOTE, note)
        assert connection.execute(text("SELECT kind, amount_minor, currency FROM cash_notes WHERE id = :id"), {"id": note["id"]}).one() == ("RECEIPT", 100, "RUB")
        with pytest.raises(DatabaseError):
            with connection.begin_nested():
                connection.execute(text("UPDATE cash_notes SET note = 'changed' WHERE id = :id"), {"id": note["id"]})


@pytest.mark.parametrize("field,value", [("amount_minor", 0), ("amount_minor", -1), ("currency", "USD"), ("kind", "PAYMENT")])
def test_cash_notes_reject_invalid_money_facts(database, field, value):
    with database.begin() as connection:
        owner_id, inquiry_id = owner_and_inquiry(connection)
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(INSERT_NOTE, note_values(owner_id, inquiry_id, **{field: value}))


def test_corrections_reject_cross_inquiry_duplicate_links_and_self_replacement(database):
    with database.begin() as connection:
        owner_id, first_inquiry = owner_and_inquiry(connection)
        _, second_inquiry = owner_and_inquiry(connection)
        original = note_values(owner_id, first_inquiry)
        replacement = note_values(owner_id, first_inquiry)
        other = note_values(owner_id, first_inquiry)
        foreign_note = note_values(owner_id, second_inquiry)
        connection.execute(INSERT_NOTE, original)
        connection.execute(INSERT_NOTE, replacement)
        connection.execute(INSERT_NOTE, other)
        connection.execute(INSERT_NOTE, foreign_note)
        correction = {"id": uuid4(), "inquiry_id": first_inquiry, "original": original["id"], "replacement": replacement["id"], "reason": "synthetic correction", "owner": owner_id}
        connection.execute(text("""INSERT INTO cash_note_corrections
            (id, inquiry_id, original_note_id, replacement_note_id, reason, owner_id)
            VALUES (:id, :inquiry_id, :original, :replacement, :reason, :owner)"""), correction)
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""INSERT INTO cash_note_corrections
                    (id, inquiry_id, original_note_id, replacement_note_id, reason, owner_id)
                    VALUES (:id, :inquiry_id, :original, :replacement, 'duplicate original', :owner)"""), {**correction, "id": uuid4(), "replacement": other["id"]})
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""INSERT INTO cash_note_corrections
                    (id, inquiry_id, original_note_id, replacement_note_id, reason, owner_id)
                    VALUES (:id, :inquiry_id, :original, :replacement, 'duplicate replacement', :owner)"""), {**correction, "id": uuid4(), "original": other["id"]})
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""INSERT INTO cash_note_corrections
                    (id, inquiry_id, original_note_id, replacement_note_id, reason, owner_id)
                    VALUES (:id, :inquiry_id, :original, :replacement, 'self replacement', :owner)"""), {**correction, "id": uuid4(), "original": other["id"], "replacement": other["id"]})
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""INSERT INTO cash_note_corrections
                    (id, inquiry_id, original_note_id, replacement_note_id, reason, owner_id)
                    VALUES (:id, :inquiry_id, :original, :replacement, 'cross inquiry', :owner)"""), {**correction, "id": uuid4(), "original": foreign_note["id"], "replacement": None})
        with pytest.raises(DatabaseError):
            with connection.begin_nested():
                connection.execute(text("UPDATE cash_note_corrections SET reason = 'changed' WHERE id = :id"), {"id": correction["id"]})
