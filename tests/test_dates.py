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
