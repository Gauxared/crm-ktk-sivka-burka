"""Transactional provisioning of identities from authenticated channel events.

The caller must authenticate the platform event before constructing a
``ChannelContext``.  This service deliberately accepts no user answers and
does not know about platform transports or conversation state.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from .channel_gateway import ChannelContext, GatewayError, validate_context
from .db import database_url_from_environment


class ChannelIdentityError(ValueError):
    """Safe typed error for identity provisioning."""

    def __init__(self, code: str, message: str = "Channel identity operation failed") -> None:
        super().__init__(message)
        self.code = code


class InvalidChannelContext(ChannelIdentityError):
    def __init__(self) -> None:
        super().__init__("INVALID_CONTEXT", "Invalid trusted channel context")


class ChannelIdentityPersistenceError(ChannelIdentityError):
    def __init__(self) -> None:
        super().__init__("IDENTITY_UNAVAILABLE", "Channel identity is temporarily unavailable")


@dataclass(frozen=True, slots=True)
class ChannelIdentityResult:
    identity_id: UUID
    contact_id: UUID
    created: bool


class ChannelIdentityService:
    """Ensure one contact-backed identity for a trusted channel sender."""

    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine or create_engine(database_url_from_environment())

    def ensure_identity(self, context: ChannelContext) -> ChannelIdentityResult:
        try:
            context = validate_context(context)
        except (GatewayError, TypeError, AttributeError):
            raise InvalidChannelContext() from None

        try:
            # The singleton guard is acquired before the read, so concurrent
            # first creates cannot observe the same absent identity.
            with self.engine.begin() as connection:
                connection.execute(text("SELECT id FROM business_write_guard WHERE id = 1 FOR UPDATE"))
                existing = connection.execute(
                    text("""SELECT id, contact_id
                           FROM channel_identities
                           WHERE platform = :platform
                             AND integration_id = :integration_id
                             AND external_sender_id = :external_sender_id"""),
                    {
                        "platform": context.platform.value,
                        "integration_id": context.integration_id,
                        "external_sender_id": context.external_sender_id,
                    },
                ).mappings().first()
                if existing is not None:
                    return ChannelIdentityResult(UUID(str(existing["id"])), UUID(str(existing["contact_id"])), False)

                identity_id, contact_id = self._insert_identity(connection, context)
                return ChannelIdentityResult(identity_id, contact_id, True)
        except SQLAlchemyError:
            raise ChannelIdentityPersistenceError() from None

    def _insert_identity(self, connection, context: ChannelContext) -> tuple[UUID, UUID]:
        contact_id = uuid4()
        identity_id = uuid4()
        connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
        connection.execute(
            text("""INSERT INTO channel_identities
                   (id, contact_id, platform, integration_id, external_sender_id)
                   VALUES (:id, :contact_id, :platform, :integration_id, :external_sender_id)"""),
            {
                "id": identity_id,
                "contact_id": contact_id,
                "platform": context.platform.value,
                "integration_id": context.integration_id,
                "external_sender_id": context.external_sender_id,
            },
        )
        return identity_id, contact_id

    # A concise adapter-facing alias keeps the application port easy to use.
    ensure = ensure_identity


TrustedChannelIdentityService = ChannelIdentityService
