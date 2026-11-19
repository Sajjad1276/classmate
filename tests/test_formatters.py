from types import SimpleNamespace

from classmate.bot.formatters import _format_homework


def test_homework_formatter_explicitly_reports_results_beyond_display_cap():
    rows = [
        (
            SimpleNamespace(title=f"Homework {index}", description="Description", due_on=None),
            SimpleNamespace(name="Mathematics"),
        )
        for index in range(1, 32)
    ]

    rendered = _format_homework(rows)

    assert "بیش از ۳۰ تکلیف" in rendered
    assert "Homework 1" in rendered
    assert "Homework 30" in rendered
    assert "Homework 31" not in rendered
    assert rendered.count("•") == 30


def test_homework_formatter_has_clear_empty_state():
    assert _format_homework([]) == "هنوز تکلیف تأییدشده‌ای ثبت نشده است."
