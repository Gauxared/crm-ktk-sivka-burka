"""Create contacts and inquiries without workflow commands.

Revision ID: 20260913_02
Revises: 20260913_01
Create Date: 2026-09-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260913_02"
down_revision = "20260913_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contact_cards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("version > 0", name="contact_cards_version_positive"),
    )

    op.create_table(
        "channel_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("integration_id", sa.Text(), nullable=False),
        sa.Column("external_sender_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["contact_cards.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("platform", "integration_id", "external_sender_id", name="channel_identities_platform_integration_sender_key"),
        sa.CheckConstraint("platform IN ('TELEGRAM', 'VK')", name="channel_identities_platform"),
        sa.CheckConstraint("integration_id <> ''", name="channel_identities_integration_not_empty"),
        sa.CheckConstraint("external_sender_id <> ''", name="channel_identities_sender_not_empty"),
    )
    op.create_index("channel_identities_contact_id_idx", "channel_identities", ["contact_id"])

    op.create_table(
        "inquiries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("contact_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("selection_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("requester_name", sa.Text(), nullable=False),
        sa.Column("contact_kind", sa.Text(), nullable=False),
        sa.Column("contact_value", sa.Text(), nullable=False),
        sa.Column("requested_date", sa.Date(), nullable=True),
        sa.Column("requested_time_text", sa.Text(), nullable=True),
        sa.Column("requested_timezone", sa.Text(), nullable=True),
        sa.Column("experience", sa.Text(), nullable=False, server_default="UNKNOWN"),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("acquisition", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(["contact_id"], ["contact_cards.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["channel_identity_id"], ["channel_identities.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("source_kind IN ('PUBLIC_FORM', 'TELEGRAM', 'VK', 'PHONE', 'WHATSAPP', 'OTHER')", name="inquiries_source_kind"),
        sa.CheckConstraint("status IN ('NEW', 'NEGOTIATING', 'CONFIRMED', 'COMPLETED', 'CANCELLED')", name="inquiries_status"),
        sa.CheckConstraint("version > 0", name="inquiries_version_positive"),
        sa.CheckConstraint("requester_name <> ''", name="inquiries_requester_name_not_empty"),
        sa.CheckConstraint("contact_kind IN ('PHONE', 'TELEGRAM', 'VK', 'OTHER')", name="inquiries_contact_kind"),
        sa.CheckConstraint("contact_value <> ''", name="inquiries_contact_value_not_empty"),
        sa.CheckConstraint("experience IN ('BEGINNER', 'EXPERIENCED', 'UNKNOWN')", name="inquiries_experience"),
    )
    op.create_index("inquiries_contact_id_idx", "inquiries", ["contact_id"])
    op.create_index("inquiries_channel_identity_id_idx", "inquiries", ["channel_identity_id"])
    op.create_index("inquiries_received_at_idx", "inquiries", ["received_at"])

    op.create_table(
        "inquiry_terms",
        sa.Column("inquiry_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("service_option_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("participants_count", sa.Integer(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("total_minor", sa.BigInteger(), nullable=True),
        sa.Column("expected_prepayment_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False, server_default="RUB"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["inquiry_id"], ["inquiries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["service_option_id"], ["service_options.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("participants_count > 0", name="inquiry_terms_participants_positive"),
        sa.CheckConstraint("duration_minutes IS NULL OR duration_minutes > 0", name="inquiry_terms_duration_positive"),
        sa.CheckConstraint("total_minor IS NULL OR total_minor BETWEEN 0 AND 9000000000000", name="inquiry_terms_total_range"),
        sa.CheckConstraint("expected_prepayment_minor IS NULL OR expected_prepayment_minor BETWEEN 0 AND 9000000000000", name="inquiry_terms_prepayment_range"),
        sa.CheckConstraint("currency = 'RUB'", name="inquiry_terms_currency_rub"),
    )
    op.create_index("inquiry_terms_service_option_id_idx", "inquiry_terms", ["service_option_id"])


def downgrade() -> None:
    op.drop_index("inquiry_terms_service_option_id_idx", table_name="inquiry_terms")
    op.drop_table("inquiry_terms")
    op.drop_index("inquiries_received_at_idx", table_name="inquiries")
    op.drop_index("inquiries_channel_identity_id_idx", table_name="inquiries")
    op.drop_index("inquiries_contact_id_idx", table_name="inquiries")
    op.drop_table("inquiries")
    op.drop_index("channel_identities_contact_id_idx", table_name="channel_identities")
    op.drop_table("channel_identities")
    op.drop_table("contact_cards")
