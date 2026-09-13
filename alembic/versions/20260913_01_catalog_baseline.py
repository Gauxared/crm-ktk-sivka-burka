"""Create the catalog and singleton write guard baseline.

Revision ID: 20260913_01
Revises:
Create Date: 2026-09-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260913_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_write_guard",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.CheckConstraint("id = 1", name="business_write_guard_singleton"),
    )
    op.execute("INSERT INTO business_write_guard (id) VALUES (1)")

    op.create_table(
        "club_settings",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("catalog_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("contact_info", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("location_link", sa.Text(), nullable=False),
        sa.Column("visit_rules", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint("id = 1", name="club_settings_singleton"),
        sa.CheckConstraint("catalog_version > 0", name="club_settings_catalog_version_positive"),
        sa.CheckConstraint("version > 0", name="club_settings_version_positive"),
    )

    op.create_table(
        "services",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("information", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("code", name="services_code_key"),
        sa.CheckConstraint("code <> ''", name="services_code_not_empty"),
        sa.CheckConstraint("title <> ''", name="services_title_not_empty"),
    )

    op.create_table(
        "service_options",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("pricing_mode", sa.Text(), nullable=False),
        sa.Column("price_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False, server_default="RUB"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("code", name="service_options_code_key"),
        sa.CheckConstraint("code <> ''", name="service_options_code_not_empty"),
        sa.CheckConstraint("duration_minutes IS NULL OR duration_minutes > 0", name="service_options_duration_positive"),
        sa.CheckConstraint("pricing_mode IN ('FIXED_PER_PERSON', 'NEGOTIATED')", name="service_options_pricing_mode"),
        sa.CheckConstraint("currency = 'RUB'", name="service_options_currency_rub"),
        sa.CheckConstraint("price_minor IS NULL OR price_minor BETWEEN 0 AND 9000000000000", name="service_options_price_range"),
        sa.CheckConstraint("(pricing_mode = 'FIXED_PER_PERSON' AND price_minor IS NOT NULL) OR (pricing_mode = 'NEGOTIATED' AND price_minor IS NULL)", name="service_options_pricing_consistency"),
    )
    op.create_index("service_options_service_id_idx", "service_options", ["service_id"])


def downgrade() -> None:
    op.drop_index("service_options_service_id_idx", table_name="service_options")
    op.drop_table("service_options")
    op.drop_table("services")
    op.drop_table("club_settings")
    op.drop_table("business_write_guard")
