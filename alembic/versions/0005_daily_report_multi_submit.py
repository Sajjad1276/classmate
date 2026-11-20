"""Allow multiple daily reports for a class on the same date."""

import sqlalchemy as sa
from alembic import op

revision = "0005_daily_report_multi_submit"
down_revision = "0004_daily_report_cancelled_at"
branch_labels = None
depends_on = None

OLD_CONSTRAINT = "uq_daily_report_class_date"
REPORT_INDEX = "ix_daily_report_class_date"


def _unique_constraints() -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_unique_constraints("daily_reports")
        if item.get("name")
    }


def _indexes() -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("daily_reports")
        if item.get("name")
    }


def upgrade() -> None:
    if OLD_CONSTRAINT in _unique_constraints():
        op.drop_constraint(OLD_CONSTRAINT, "daily_reports", type_="unique")
    if REPORT_INDEX not in _indexes():
        op.create_index(
            REPORT_INDEX,
            "daily_reports",
            ["class_id", "report_date"],
            unique=False,
        )


def downgrade() -> None:
    # Do not silently discard submissions or fail by reintroducing a conflicting
    # unique constraint while multiple reports exist for a class/date.
    duplicate = op.get_bind().execute(
        sa.text(
            "SELECT class_id, report_date FROM daily_reports "
            "GROUP BY class_id, report_date HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            "Cannot restore uq_daily_report_class_date while multiple daily reports "
            "exist for the same class and date; consolidate them first."
        )
    if REPORT_INDEX in _indexes():
        op.drop_index(REPORT_INDEX, table_name="daily_reports")
    if OLD_CONSTRAINT not in _unique_constraints():
        op.create_unique_constraint(
            OLD_CONSTRAINT,
            "daily_reports",
            ["class_id", "report_date"],
        )
