"""Create idempotency receipts and channel event identity.

Revision ID: 20260913_04
Revises: 20260913_03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260913_04"
down_revision = "20260913_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=False),
        sa.Column("key_digest", sa.LargeBinary(), nullable=False),
        sa.Column("payload_digest", sa.LargeBinary(), nullable=False),
        sa.Column("digest_key_version", sa.Integer(), nullable=False),
        sa.Column("normalization_version", sa.Integer(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("scope_kind", "scope_id", "method", "canonical_path", "key_digest", name="operation_receipts_identity_key"),
        sa.CheckConstraint("scope_kind <> '' AND scope_id <> '' AND method <> ''", name="operation_receipts_scope_not_empty"),
        sa.CheckConstraint("canonical_path LIKE '/%'", name="operation_receipts_canonical_path"),
        sa.CheckConstraint("octet_length(key_digest) > 0 AND octet_length(payload_digest) > 0", name="operation_receipts_digests_not_empty"),
        sa.CheckConstraint("digest_key_version > 0 AND normalization_version > 0", name="operation_receipts_versions_positive"),
        sa.CheckConstraint("result_expires_at IS NULL OR result_expires_at >= accepted_at", name="operation_receipts_result_expiry"),
        sa.CheckConstraint("result_json IS NOT NULL OR tombstoned_at IS NOT NULL", name="operation_receipts_completed"),
        sa.CheckConstraint("tombstoned_at IS NULL OR (result_json IS NULL AND result_expires_at IS NULL AND tombstoned_at >= accepted_at)", name="operation_receipts_tombstone_state"),
    )
    op.create_table(
        "submission_tokens",
        sa.Column("token_digest", sa.LargeBinary(), primary_key=True),
        sa.Column("digest_key_version", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_receipt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["used_receipt_id"], ["operation_receipts.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("octet_length(token_digest) > 0 AND digest_key_version > 0", name="submission_tokens_digest"),
        sa.CheckConstraint("expires_at > issued_at", name="submission_tokens_expiry"),
        sa.CheckConstraint("(used_receipt_id IS NULL AND consumed_at IS NULL) OR (used_receipt_id IS NOT NULL AND consumed_at IS NOT NULL AND consumed_at >= issued_at AND consumed_at <= expires_at)", name="submission_tokens_consumption"),
    )
    op.create_table(
        "receipt_inquiries",
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inquiry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["receipt_id"], ["operation_receipts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["inquiry_id"], ["inquiries.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("receipt_id", "inquiry_id"),
    )
    op.create_index("receipt_inquiries_inquiry_id_idx", "receipt_inquiries", ["inquiry_id"])
    op.create_table(
        "channel_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("integration_id", sa.Text(), nullable=False),
        sa.Column("event_key_digest", sa.LargeBinary(), nullable=False),
        sa.Column("dialog_key_digest", sa.LargeBinary(), nullable=False),
        sa.Column("digest_key_version", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("outcome_kind", sa.Text(), nullable=False),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["receipt_id"], ["operation_receipts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("platform", "integration_id", "event_key_digest", name="channel_events_identity_key"),
        sa.CheckConstraint("platform IN ('TELEGRAM', 'VK') AND integration_id <> '' AND outcome_kind <> ''", name="channel_events_identity"),
        sa.CheckConstraint("octet_length(event_key_digest) > 0 AND octet_length(dialog_key_digest) > 0 AND digest_key_version > 0", name="channel_events_digests"),
    )
    op.create_table(
        "conversation_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel_identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("answers", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["channel_identity_id"], ["channel_identities.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("version > 0 AND conversation_id <> '' AND step <> ''", name="conversation_drafts_values"),
        sa.CheckConstraint("jsonb_typeof(answers) = 'object'", name="conversation_drafts_answers_object"),
    )
    op.create_index("one_open_conversation_draft", "conversation_drafts", ["channel_identity_id", "conversation_id"], unique=True, postgresql_where=sa.text("closed_at IS NULL"))
    op.create_index("open_conversation_drafts_expiry_idx", "conversation_drafts", ["expires_at"], postgresql_where=sa.text("closed_at IS NULL"))


def downgrade() -> None:
    op.drop_index("open_conversation_drafts_expiry_idx", table_name="conversation_drafts")
    op.drop_index("one_open_conversation_draft", table_name="conversation_drafts")
    op.drop_table("conversation_drafts")
    op.drop_table("channel_events")
    op.drop_index("receipt_inquiries_inquiry_id_idx", table_name="receipt_inquiries")
    op.drop_table("receipt_inquiries")
    op.drop_table("submission_tokens")
    op.drop_table("operation_receipts")
