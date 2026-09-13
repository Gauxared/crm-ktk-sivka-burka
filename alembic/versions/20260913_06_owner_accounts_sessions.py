"""Persist the single owner account and revocable sessions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260913_06"
down_revision = "20260913_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owner_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.LargeBinary(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credentials_version", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("login", name="owner_accounts_login_key"),
        sa.CheckConstraint("login <> ''", name="owner_accounts_login_not_empty"),
        sa.CheckConstraint("octet_length(password_hash) > 0", name="owner_accounts_password_hash_not_empty"),
        sa.CheckConstraint("credentials_version > 0", name="owner_accounts_credentials_version_positive"),
    )
    op.create_table(
        "owner_sessions",
        sa.Column("token_digest", sa.LargeBinary(), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credentials_version", sa.BigInteger(), nullable=False),
        sa.Column("csrf_secret", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["owner_accounts.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("octet_length(token_digest) > 0", name="owner_sessions_token_digest_not_empty"),
        sa.CheckConstraint("credentials_version > 0", name="owner_sessions_credentials_version_positive"),
        sa.CheckConstraint("octet_length(csrf_secret) > 0", name="owner_sessions_csrf_secret_not_empty"),
        sa.CheckConstraint("expires_at > created_at", name="owner_sessions_expiry_after_creation"),
        sa.CheckConstraint("last_seen_at >= created_at", name="owner_sessions_last_seen_after_creation"),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= created_at", name="owner_sessions_revoked_after_creation"),
    )
    op.create_index("owner_sessions_expires_at_idx", "owner_sessions", ["expires_at", "token_digest"])


def downgrade() -> None:
    op.drop_index("owner_sessions_expires_at_idx", table_name="owner_sessions")
    op.drop_table("owner_sessions")
    op.drop_table("owner_accounts")
