from sqlalchemy import UniqueConstraint

from classmate.db.models import DailyReport


def test_daily_reports_allow_multiple_submissions_per_class_and_date():
    unique_constraints = [
        constraint
        for constraint in DailyReport.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
        and {column.name for column in constraint.columns}
        == {"class_id", "report_date"}
    ]
    index_names = {index.name for index in DailyReport.__table__.indexes}

    assert unique_constraints == []
    assert "ix_daily_report_class_date" in index_names
