"""Create shared visits and historical inquiry participations.

Revision ID: 20260913_03
Revises: 20260913_02
Create Date: 2026-09-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260913_03"
down_revision = "20260913_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "visits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('PLANNED', 'COMPLETED', 'CANCELLED')", name="visits_status"),
        sa.CheckConstraint("version > 0", name="visits_version_positive"),
        sa.CheckConstraint("duration_minutes IS NULL OR duration_minutes > 0", name="visits_duration_positive"),
    )
    op.create_index("visits_service_id_idx", "visits", ["service_id"])
    op.create_index("visits_start_at_idx", "visits", ["start_at"])

    op.create_table(
        "visit_participations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("inquiry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("visit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_kind", sa.Text(), nullable=True),
        sa.Column("participants_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("plan_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("completion_voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_void_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["inquiry_id"], ["inquiries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("(closed_at IS NULL AND close_kind IS NULL) OR (closed_at IS NOT NULL AND close_kind IN ('MOVED', 'UNSCHEDULED', 'CANCELLED', 'PROVIDED'))", name="visit_participations_closed_state"),
        sa.CheckConstraint("closed_at IS NULL OR closed_at >= joined_at", name="visit_participations_closed_after_joined"),
        sa.CheckConstraint("closed_at IS NULL OR (participants_snapshot IS NOT NULL AND plan_snapshot IS NOT NULL)", name="visit_participations_closed_snapshots"),
        sa.CheckConstraint("(completion_voided_at IS NULL AND completion_void_reason IS NULL) OR (closed_at IS NOT NULL AND close_kind = 'PROVIDED' AND completion_voided_at IS NOT NULL AND completion_void_reason IS NOT NULL AND completion_void_reason <> '')", name="visit_participations_completion_void"),
    )
    op.create_index("visit_participations_inquiry_id_idx", "visit_participations", ["inquiry_id"])
    op.create_index("visit_participations_visit_id_idx", "visit_participations", ["visit_id"])
    op.create_index("one_active_participation_per_inquiry", "visit_participations", ["inquiry_id"], unique=True, postgresql_where=sa.text("closed_at IS NULL"))


def downgrade() -> None:
    op.drop_index("one_active_participation_per_inquiry", table_name="visit_participations")
    op.drop_index("visit_participations_visit_id_idx", table_name="visit_participations")
    op.drop_index("visit_participations_inquiry_id_idx", table_name="visit_participations")
    op.drop_table("visit_participations")
    op.drop_index("visits_start_at_idx", table_name="visits")
    op.drop_index("visits_service_id_idx", table_name="visits")
    op.drop_table("visits")
