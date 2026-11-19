from datetime import date, datetime, time, timezone, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from classmate.domain.dates import normalize_digits, parse_school_date, persian_weekday
from classmate.services.records import (
    local_day_window,
    next_timetable_occurrence,
    normalize_subject_name,
)


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


def test_next_timetable_occurrence_respects_future_valid_from():
    now = datetime(2026, 11, 19, 12, 0, tzinfo=ZoneInfo("Asia/Tehran"))
    entry = SimpleNamespace(
        weekday=0,
        valid_from=date(2026, 12, 19),
        valid_until=None,
        starts_at=time(9, 0),
        ends_at=time(9, 45),
    )

    occurrence = next_timetable_occurrence(entry, now)

    assert occurrence is not None
    starts_at, ends_at = occurrence
    assert starts_at == datetime(2026, 12, 19, 9, 0, tzinfo=ZoneInfo("Asia/Tehran"))
    assert ends_at == datetime(2026, 12, 19, 9, 45, tzinfo=ZoneInfo("Asia/Tehran"))


def test_next_timetable_occurrence_moves_to_next_week_when_today_has_passed():
    now = datetime(2026, 11, 21, 10, 0, tzinfo=ZoneInfo("Asia/Tehran"))
    entry = SimpleNamespace(
        weekday=0,
        valid_from=None,
        valid_until=None,
        starts_at=time(9, 0),
        ends_at=None,
    )

    occurrence = next_timetable_occurrence(entry, now)

    assert occurrence is not None
    assert occurrence[0].date() == date(2026, 11, 28)


def test_next_timetable_occurrence_stops_after_valid_until():
    now = datetime(2026, 11, 19, 12, 0, tzinfo=ZoneInfo("Asia/Tehran"))
    entry = SimpleNamespace(
        weekday=0,
        valid_from=None,
        valid_until=date(2026, 11, 20),
        starts_at=time(9, 0),
        ends_at=None,
    )

    assert next_timetable_occurrence(entry, now) is None



def test_local_day_window_tracks_daylight_saving_transition():
    start, end = local_day_window(date(2026, 3, 29), "Europe/Amsterdam")

    assert start == datetime(2026, 3, 29, 0, 0, tzinfo=start.tzinfo)
    assert end == datetime(2026, 3, 30, 0, 0, tzinfo=end.tzinfo)
    assert (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)) == __import__("datetime").timedelta(hours=23)
