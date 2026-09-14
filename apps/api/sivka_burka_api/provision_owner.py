"""Deployment-time provisioning of the single initial owner account.

This module deliberately has no HTTP dependencies.  It is intended to be run
once by an operator during deployment, with the database URL supplied only by
the process environment.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from uuid import uuid4

from sqlalchemy import create_engine, text

from sivka_burka_api.admin_sessions import hash_password


class ProvisioningError(RuntimeError):
    """A safe, operator-facing provisioning failure."""


def provision_owner(database_url: str, login: str, password: str, confirmation: str) -> str:
    """Create exactly one owner in one transaction and return its UUID.

    The caller must validate the two passwords before calling this function.
    The table lock serializes competing provisioning attempts and the count is
    checked while that lock is held.
    """
    if not database_url:
        raise ProvisioningError("DATABASE_URL is required")
    if not login or not login.strip():
        raise ProvisioningError("login must not be empty")
    if password != confirmation:
        raise ProvisioningError("passwords do not match")
    if not password:
        raise ProvisioningError("password must not be empty")

    engine = create_engine(database_url)
    owner_id = uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(text("LOCK TABLE owner_accounts IN ACCESS EXCLUSIVE MODE"))
            if connection.execute(text("SELECT 1 FROM owner_accounts LIMIT 1")).scalar() is not None:
                raise ProvisioningError("an owner account already exists")
            connection.execute(
                text("""INSERT INTO owner_accounts
                    (id, login, password_hash, credentials_version)
                    VALUES (:id, :login, :password_hash, :credentials_version)"""),
                {"id": owner_id, "login": login.strip(), "password_hash": hash_password(password), "credentials_version": 1},
            )
    finally:
        engine.dispose()
    return str(owner_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Provision the first Sivka-Burka owner account.")
    parser.add_argument("login", help="non-empty owner login")
    parser.add_argument("--confirm", action="store_true", help="confirm this one-time provisioning operation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.confirm:
        print("Refusing to provision without --confirm", file=sys.stderr)
        return 2
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    if not args.login.strip():
        print("login must not be empty", file=sys.stderr)
        return 2
    password = getpass.getpass("Owner password: ")
    confirmation = getpass.getpass("Repeat owner password: ")
    try:
        owner_id = provision_owner(database_url, args.login, password, confirmation)
    except ProvisioningError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Owner provisioned: id={owner_id} login={args.login.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
