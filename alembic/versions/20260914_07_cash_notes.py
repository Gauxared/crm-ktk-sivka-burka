"""Persist immutable manual cash notes and their corrections."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260914_07"
down_revision = "20260913_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cash_notes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("inquiry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default="RUB"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["inquiry_id"], ["inquiries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner_accounts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("inquiry_id", "id", name="cash_notes_inquiry_id_id_key"),
        sa.CheckConstraint("kind IN ('RECEIPT', 'REFUND_NOTE')", name="cash_notes_kind"),
        sa.CheckConstraint("amount_minor BETWEEN 1 AND 9000000000000", name="cash_notes_amount_range"),
        sa.CheckConstraint("currency = 'RUB'", name="cash_notes_currency_rub"),
    )
    op.create_index("cash_notes_owner_id_idx", "cash_notes", ["owner_id", "id"])

    op.create_table(
        "cash_note_corrections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("inquiry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_note_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("replacement_note_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["inquiry_id"], ["inquiries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["inquiry_id", "original_note_id"], ["cash_notes.inquiry_id", "cash_notes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["inquiry_id", "replacement_note_id"], ["cash_notes.inquiry_id", "cash_notes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner_accounts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("original_note_id", name="cash_note_corrections_original_note_id_key"),
        sa.UniqueConstraint("replacement_note_id", name="cash_note_corrections_replacement_note_id_key"),
        sa.CheckConstraint("replacement_note_id IS NULL OR original_note_id <> replacement_note_id", name="cash_note_corrections_distinct_notes"),
    )
    op.create_index("cash_note_corrections_inquiry_id_idx", "cash_note_corrections", ["inquiry_id", "id"])
    op.create_index("cash_note_corrections_owner_id_idx", "cash_note_corrections", ["owner_id", "id"])

    op.execute(
        """
        CREATE FUNCTION reject_cash_history_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'cash history is immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER cash_notes_immutable BEFORE UPDATE OR DELETE ON cash_notes "
        "FOR EACH ROW EXECUTE FUNCTION reject_cash_history_mutation()"
    )
    op.execute(
        "CREATE TRIGGER cash_note_corrections_immutable BEFORE UPDATE OR DELETE ON cash_note_corrections "
        "FOR EACH ROW EXECUTE FUNCTION reject_cash_history_mutation()"
    )


def downgrade() -> None:
    op.drop_index("cash_note_corrections_owner_id_idx", table_name="cash_note_corrections")
    op.drop_index("cash_note_corrections_inquiry_id_idx", table_name="cash_note_corrections")
    op.drop_table("cash_note_corrections")
    op.drop_index("cash_notes_owner_id_idx", table_name="cash_notes")
    op.drop_table("cash_notes")
    op.execute("DROP FUNCTION reject_cash_history_mutation()")
