"""HTTP composition dependencies for owner commands.

Production composition deliberately remains outside this module.  The route
obtains the authenticated owner identity from the session boundary, then uses
this runtime to construct the existing transactional command service.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine

from .owner_commands import OwnerCommandService


@dataclass(frozen=True)
class AdminCommandRuntime:
    """Explicit infrastructure and digest material for owner commands."""

    engine: Engine
    hmac_secret: bytes
    digest_key_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.engine, Engine):
            raise ValueError("engine must be injected")
        if not isinstance(self.hmac_secret, bytes) or not self.hmac_secret:
            raise ValueError("hmac_secret must be an injected non-empty bytes value")
        if (
            not isinstance(self.digest_key_version, int)
            or isinstance(self.digest_key_version, bool)
            or self.digest_key_version <= 0
        ):
            raise ValueError("digest_key_version must be positive")

    def owner_commands(self, owner_id: UUID) -> OwnerCommandService:
        return OwnerCommandService(
            self.engine,
            owner_id,
            hmac_secret=self.hmac_secret,
            digest_key_version=self.digest_key_version,
        )
