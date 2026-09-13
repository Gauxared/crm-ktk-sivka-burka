"""Create inquiry audit events and notification outbox persistence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260913_05"
down_revision = "20260913_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "change_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["command_id"], ["operation_receipts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("command_id", "ordinal", name="change_events_command_ordinal_key"),
        sa.CheckConstraint("ordinal > 0", name="change_events_ordinal_positive"),
        sa.CheckConstraint("actor_kind IN ('OWNER', 'CHANNEL', 'SYSTEM')", name="change_events_actor_kind"),
        sa.CheckConstraint("action <> ''", name="change_events_action_not_empty"),
        sa.CheckConstraint("schema_version > 0", name="change_events_schema_version_positive"),
        sa.CheckConstraint("jsonb_typeof(changes) = 'object'", name="change_events_changes_object"),
    )
    op.create_index("change_events_recorded_at_idx", "change_events", ["recorded_at", "id"])

    op.create_table(
        "event_inquiries",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inquiry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["change_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["inquiry_id"], ["inquiries.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id", "inquiry_id"),
    )
    op.create_index("event_inquiries_inquiry_id_idx", "event_inquiries", ["inquiry_id", "event_id"])

    op.create_table(
        "event_visits",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("visit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["change_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id", "visit_id"),
    )
    op.create_index("event_visits_visit_id_idx", "event_visits", ["visit_id", "event_id"])

    op.create_table(
        "notification_recipient_state",
        sa.Column("recipient_ref", sa.Text(), primary_key=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_allowed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("recipient_ref <> ''", name="notification_recipient_ref_not_empty"),
        sa.CheckConstraint(
            "(lease_token IS NULL AND lease_until IS NULL) OR (lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="notification_recipient_lease_pair",
        ),
    )

    op.create_table(
        "notification_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("origin_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inquiry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_ref", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, server_default="OWNER_INQUIRY_RECEIVED"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cycle_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cycle_started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("cycle_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["origin_event_id"], ["change_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["inquiry_id"], ["inquiries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipient_ref"], ["notification_recipient_state.recipient_ref"], ondelete="RESTRICT"),
        sa.UniqueConstraint("origin_event_id", "recipient_ref", "kind", name="notification_jobs_origin_recipient_kind_key"),
        sa.CheckConstraint("kind = 'OWNER_INQUIRY_RECEIVED'", name="notification_jobs_kind"),
        sa.CheckConstraint("version > 0 AND attempt_count >= 0 AND cycle_no > 0", name="notification_jobs_counters"),
        sa.CheckConstraint("cycle_attempt_count BETWEEN 0 AND 8 AND attempt_count >= cycle_attempt_count", name="notification_jobs_cycle_attempts"),
        sa.CheckConstraint("payload_schema_version > 0", name="notification_jobs_payload_schema_version"),
        sa.CheckConstraint("status IN ('PENDING', 'SENDING', 'RETRY_WAIT', 'SENT', 'BLOCKED', 'FAILED', 'SUPPRESSED')", name="notification_jobs_status"),
        sa.CheckConstraint(
            "(status = 'SENDING') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="notification_jobs_sending_lease",
        ),
        sa.CheckConstraint("status = 'SENT' OR delivered_at IS NULL", name="notification_jobs_delivered_state"),
        sa.CheckConstraint("status = 'SENT' OR recipient_ref <> ''", name="notification_jobs_recipient_ref"),
    )
    op.create_index("notification_jobs_status_due_idx", "notification_jobs", ["status", "next_attempt_at", "id"])
    op.create_index("notification_jobs_recipient_status_idx", "notification_jobs", ["recipient_ref", "status"])

    op.create_table(
        "notification_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cycle_no", sa.Integer(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_code", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["notification_jobs.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("job_id", "cycle_no", "attempt_no", name="notification_attempts_job_cycle_attempt_key"),
        sa.CheckConstraint("cycle_no > 0 AND attempt_no BETWEEN 1 AND 8", name="notification_attempts_numbers"),
        sa.CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="notification_attempts_finished_after_started"),
        sa.CheckConstraint("finished_at IS NOT NULL OR outcome_code IS NULL", name="notification_attempts_open_outcome"),
    )
    op.create_index("notification_attempts_job_idx", "notification_attempts", ["job_id", "cycle_no", "attempt_no"])


def downgrade() -> None:
    op.drop_index("notification_attempts_job_idx", table_name="notification_attempts")
    op.drop_table("notification_attempts")
    op.drop_index("notification_jobs_recipient_status_idx", table_name="notification_jobs")
    op.drop_index("notification_jobs_status_due_idx", table_name="notification_jobs")
    op.drop_table("notification_jobs")
    op.drop_table("notification_recipient_state")
    op.drop_index("event_visits_visit_id_idx", table_name="event_visits")
    op.drop_table("event_visits")
    op.drop_index("event_inquiries_inquiry_id_idx", table_name="event_inquiries")
    op.drop_table("event_inquiries")
    op.drop_index("change_events_recorded_at_idx", table_name="change_events")
    op.drop_table("change_events")
