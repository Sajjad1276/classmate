from types import SimpleNamespace

from classmate.bot.formatters import _format_exams, _format_homework


def test_homework_formatter_caps_items_and_reports_omitted_count():
    rows = [
        (
            SimpleNamespace(
                title=f"Homework {index}",
                description="Long description " * 30,
                due_on=None,
            ),
            SimpleNamespace(name="Mathematics"),
        )
        for index in range(1, 32)
    ]

    rendered = _format_homework(rows)

    assert "Homework 1" in rendered
    assert "Homework 8" in rendered
    assert "Homework 9" not in rendered
    assert "23 تکلیف دیگر نمایش داده نشد" in rendered
    assert rendered.count("•") == 8
    assert "\\n" not in rendered
    assert "\n" in rendered
    assert len(rendered.encode("utf-16-le")) // 2 < 3900


def test_homework_formatter_has_clear_empty_state():
    assert _format_homework([]) == "هنوز تکلیف تأییدشده‌ای ثبت نشده است."


def test_exam_formatter_caps_items_and_reports_omitted_count():
    from datetime import datetime, timezone

    rows = [
        (
            SimpleNamespace(
                title=f"Exam {index}",
                scope="Scope " * 60,
                scheduled_at=datetime(2026, 12, 1, tzinfo=timezone.utc),
            ),
            SimpleNamespace(name="Physics"),
        )
        for index in range(1, 16)
    ]
    rendered = _format_exams(rows, "Asia/Tehran")
    assert "Exam 8" in rendered
    assert "Exam 9" not in rendered
    assert "7 امتحان دیگر نمایش داده نشد" in rendered
    assert "\\n" not in rendered
    assert "\n" in rendered
    assert len(rendered.encode("utf-16-le")) // 2 < 3900
