from datetime import date

import pytest

from classmate.domain.dates import normalize_digits, parse_school_date, persian_weekday
from classmate.services.records import normalize_subject_name


def test_parse_jalali_date():
    assert parse_school_date("۱۴۰۵/۰۸/۲۷") == date(2026, 11, 18)


def test_parse_gregorian_date():
    assert parse_school_date("2026/11/19") == date(2026, 11, 19)


def test_normalize_digits():
    assert normalize_digits("۱۴۰۵/۰۸/۲۷") == "1405/08/27"


def test_normalize_subject_name():
    assert normalize_subject_name("عربي  و  رياضي") == "عربی و ریاضی"


@pytest.mark.parametrize(
    ("weekday", "expected"),
    [
        (date(2026, 11, 21), 0),
        (date(2026, 11, 22), 1),
        (date(2026, 11, 27), 6),
    ],
)
def test_persian_weekday(weekday, expected):
    assert persian_weekday(weekday) == expected



def test_next_timetable_occurrence_searches_from_valid_from_without_mutating_base():
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from classmate.domain.dates import next_timetable_occurrence

    now = datetime(2026, 11, 20, 9, 0, tzinfo=ZoneInfo("Asia/Tehran"))
    starts_at, ends_at = next_timetable_occurrence(
        now=now,
        weekday=0,  # Saturday
        starts_at=time(8, 0),
        ends_at=time(8, 45),
        valid_from=date(2026, 11, 25),
    )
    assert starts_at.date() == date(2026, 11, 28)
    assert starts_at.time() == time(8, 0)
    assert ends_at.time() == time(8, 45)


def test_next_timetable_occurrence_skips_today_when_lesson_time_has_passed():
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from classmate.domain.dates import next_timetable_occurrence

    now = datetime(2026, 11, 21, 9, 0, tzinfo=ZoneInfo("Asia/Tehran"))
    occurrence = next_timetable_occurrence(now, 0, time(8, 0))
    assert occurrence is not None
    assert occurrence[0].date() == date(2026, 11, 28)


def test_next_timetable_occurrence_respects_valid_until():
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from classmate.domain.dates import next_timetable_occurrence

    now = datetime(2026, 11, 22, 9, 0, tzinfo=ZoneInfo("Asia/Tehran"))
    assert next_timetable_occurrence(
        now, 0, time(8, 0), valid_until=date(2026, 11, 27)
    ) is None
