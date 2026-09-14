from __future__ import annotations

from unittest.mock import patch

from sivka_burka_api import provision_owner
from sivka_burka_api.admin_sessions import verify_password


def test_cli_requires_confirmation_without_reading_password(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://synthetic/test")
    with patch.object(provision_owner.getpass, "getpass") as prompt:
        assert provision_owner.main(["owner"]) == 2
    assert not prompt.called
    assert "password" not in capsys.readouterr().out.lower()


def test_cli_reads_password_twice_and_returns_non_secret_receipt(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://synthetic/test")
    with patch.object(provision_owner.getpass, "getpass", side_effect=["synthetic-password", "synthetic-password"]), patch.object(
        provision_owner, "provision_owner", return_value="00000000-0000-0000-0000-000000000001"
    ) as provision:
        assert provision_owner.main(["owner", "--confirm"]) == 0
    provision.assert_called_once_with("postgresql+psycopg://synthetic/test", "owner", "synthetic-password", "synthetic-password")
    output = capsys.readouterr().out
    assert "synthetic-password" not in output
    assert "00000000-0000-0000-0000-000000000001" in output


def test_mismatch_is_rejected_before_engine_creation():
    with patch.object(provision_owner, "create_engine") as engine:
        try:
            provision_owner.provision_owner("postgresql+psycopg://synthetic/test", "owner", "one", "two")
        except provision_owner.ProvisioningError as error:
            assert str(error) == "passwords do not match"
        else:
            raise AssertionError("mismatch was accepted")
    engine.assert_not_called()


def test_password_hash_is_versioned_and_verifiable():
    password_hash = provision_owner.hash_password("synthetic-password")
    assert password_hash.startswith(b"scrypt$v=1$")
    assert password_hash != b"synthetic-password"
    assert verify_password(password_hash, "synthetic-password")
    assert not verify_password(password_hash, "wrong-password")
