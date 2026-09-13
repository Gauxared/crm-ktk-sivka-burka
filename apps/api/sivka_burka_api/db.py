"""Database configuration helpers shared by migration and future runtime layers."""

from __future__ import annotations

import os


class DatabaseConfigurationError(ValueError):
    """Raised when a database operation has no explicit connection target."""


def database_url_from_environment() -> str:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise DatabaseConfigurationError("DATABASE_URL is required for database operations")
    return database_url
