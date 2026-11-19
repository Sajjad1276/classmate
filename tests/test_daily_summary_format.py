from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from classmate.bot.handlers import _format_daily_summary


def test_daily_summary_is_bounded_and_reports_overflow():
    reports = [
        SimpleNamespace(
            title=f"Report {index}",
            body="<unsafe>&" + ("<&>🙂" * 300),
            report_date=date(2026, 11, 19),
        )
        for index in range(8)
    ]
    homework = [
        (
            SimpleNamespace(
                title=f"Homework {index}",
                description="<&>🙂" * 300,
                due_on=date(2026, 11, 19),
            ),
            SimpleNamespace(name=f"Subject {index}"),
        )
        for index in range(9)
    ]
    exams = [
        (
            SimpleNamespace(
                title=f"Exam {index}",
                scope="<&>🙂" * 300,
                scheduled_at=datetime(
                    2026, 11, 19, 10, 0, tzinfo=ZoneInfo("Asia/Tehran")
                ),
            ),
            SimpleNamespace(name=f"Subject {index}"),
        )
        for index in range(9)
    ]

    result = _format_daily_summary(
        reports, homework, exams, date(2026, 11, 19), "Asia/Tehran"
    )

    assert len(result.encode("utf-16-le")) // 2 < 4096
    assert "\\n" not in result
    assert "موارد بیشتری وجود دارد" in result
    assert "<unsafe>" not in result
    assert "&lt;unsafe&gt;" in result
    assert "Report 3" not in result
    assert "Homework 4" not in result
    assert "Exam 4" not in result


def test_daily_summary_handles_empty_categories():
    result = _format_daily_summary(
        [], [], [], date(2026, 11, 19), "Asia/Tehran"
    )

    assert "گزارش تأییدشده‌ای" in result
    assert "امروز تکلیف موعدداری ثبت نشده است." in result
    assert "امروز امتحانی ثبت نشده است." in result
