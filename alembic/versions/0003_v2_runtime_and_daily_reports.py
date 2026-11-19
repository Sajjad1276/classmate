"""Persist Telegram form state and allow independent daily reports."""

from alembic import op
import sqlalchemy as sa

revision = "0003_v2_runtime"
down_revision = "0002_school_group"
branch_labels = None
depends_on = None

OLD_REPORT_UNIQUE = "uq_daily_report_class_date"
REPORT_INDEX = "ix_daily_report_class_date"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    unique_names = {
        item.get("name")
        for item in inspector.get_unique_constraints("daily_reports")
        if item.get("name")
    }
    indexes = {
        item.get("name")
        for item in inspector.get_indexes("daily_reports")
        if item.get("name")
    }
    if OLD_REPORT_UNIQUE in unique_names:
        op.drop_constraint(OLD_REPORT_UNIQUE, "daily_reports", type_="unique")
    if REPORT_INDEX not in indexes:
        op.create_index(
            REPORT_INDEX,
            "daily_reports",
            ["class_id", "report_date"],
            unique=False,
        )

    tables = set(inspector.get_table_names())
    if "bot_states" not in tables:
        op.create_table(
            "bot_states",
            sa.Column("key", sa.String(length=512), primary_key=True),
            sa.Column("state", sa.String(length=160), nullable=True),
            sa.Column("data", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_bot_states_expires_at", "bot_states", ["expires_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "bot_states" in inspector.get_table_names():
        op.drop_index("ix_bot_states_expires_at", table_name="bot_states")
        op.drop_table("bot_states")

    indexes = {
        item.get("name")
        for item in sa.inspect(op.get_bind()).get_indexes("daily_reports")
        if item.get("name")
    }
    if REPORT_INDEX in indexes:
        op.drop_index(REPORT_INDEX, table_name="daily_reports")
    duplicate = op.get_bind().execute(
        sa.text(
            "SELECT class_id, report_date FROM daily_reports "
            "GROUP BY class_id, report_date HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            "Cannot restore the single-report-per-day constraint while duplicate "
            "class/date reports exist. Consolidate reports before downgrading."
        )
    current = {
        item.get("name")
        for item in sa.inspect(op.get_bind()).get_unique_constraints("daily_reports")
        if item.get("name")
    }
    if OLD_REPORT_UNIQUE not in current:
        op.create_unique_constraint(
            OLD_REPORT_UNIQUE,
            "daily_reports",
            ["class_id", "report_date"],
        )
