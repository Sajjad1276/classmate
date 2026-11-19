"""Persist FSM state, deduplicate Telegram updates, and add soft-delete markers."""

from alembic import op
import sqlalchemy as sa

revision = "0003_reliability_state"
down_revision = "0002_school_group"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    columns = _columns("processed_updates")
    if "status" not in columns:
        op.add_column(
            "processed_updates",
            sa.Column("status", sa.String(length=16), nullable=False, server_default="processed"),
        )
    if "lease_expires_at" not in columns:
        op.add_column("processed_updates", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    if "attempts" not in columns:
        op.add_column(
            "processed_updates",
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        )
    if "last_error" not in columns:
        op.add_column("processed_updates", sa.Column("last_error", sa.Text()))
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("processed_updates")}
    if "ix_processed_updates_lease_expires_at" not in indexes:
        op.create_index("ix_processed_updates_lease_expires_at", "processed_updates", ["lease_expires_at"])

    for table_name in ("subjects", "class_subjects", "timetable_entries"):
        columns = _columns(table_name)
        if "deleted_at" not in columns:
            op.add_column(table_name, sa.Column("deleted_at", sa.DateTime(timezone=True)))
        index_name = f"ix_{table_name}_deleted_at"
        indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table_name)}
        if index_name not in indexes:
            op.create_index(index_name, table_name, ["deleted_at"])

    if not sa.inspect(op.get_bind()).has_table("fsm_state_records"):
        op.create_table(
            "fsm_state_records",
            sa.Column("key", sa.String(length=512), primary_key=True),
            sa.Column("state", sa.String(length=255), nullable=True),
            sa.Column("data", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    # FSM data and idempotency history are operational state; dropping them silently
    # would reset live conversations and reopen already processed Telegram updates.
    raise RuntimeError(
        "Revision 0003 is intentionally non-reversible: it contains persisted FSM and update history. "
        "Restore from a database backup or write an explicit data-preserving migration."
    )
