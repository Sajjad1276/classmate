"""Add a cancellation timestamp to daily reports."""

import sqlalchemy as sa
from alembic import op

revision = "0004_daily_report_cancelled_at"
down_revision = "0003_reliability_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("daily_reports"):
        return
    columns = {column["name"] for column in inspector.get_columns("daily_reports")}
    if "cancelled_at" not in columns:
        op.add_column(
            "daily_reports",
            sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Revision 0004 is intentionally non-reversible because it stores record cancellation history. "
        "Use a reviewed data-preserving migration if a rollback is required."
    )
