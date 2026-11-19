"""Allow multiple daily reports for a class on the same date."""

import sqlalchemy as sa
from alembic import op

revision = "0003_daily_report_multi_submit"
down_revision = "0002_school_group"
branch_labels = None
depends_on = None

OLD_CONSTRAINT = "uq_daily_report_class_date"
NEW_INDEX = "ix_daily_report_class_date"


def _unique_constraints() -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints("daily_reports")
        if constraint.get("name")
    }


def _indexes() -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("daily_reports")
        if index.get("name")
    }


def upgrade() -> None:
    if OLD_CONSTRAINT in _unique_constraints():
        op.drop_constraint(OLD_CONSTRAINT, "daily_reports", type_="unique")
    if NEW_INDEX not in _indexes():
        op.create_index(
            NEW_INDEX,
            "daily_reports",
            ["class_id", "report_date"],
            unique=False,
        )


def downgrade() -> None:
    if NEW_INDEX in _indexes():
        op.drop_index(NEW_INDEX, table_name="daily_reports")
    duplicate = op.get_bind().execute(
        sa.text(
            "SELECT class_id, report_date FROM daily_reports "
            "GROUP BY class_id, report_date HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            "Cannot restore uq_daily_report_class_date while multiple daily reports "
            "exist for the same class and date; consolidate those records first."
        )
    if OLD_CONSTRAINT not in _unique_constraints():
        op.create_unique_constraint(
            OLD_CONSTRAINT,
            "daily_reports",
            ["class_id", "report_date"],
        )
