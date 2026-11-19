import pytest

from classmate.domain.errors import ValidationError
from classmate.domain.text import normalize_digits, normalize_name, parse_positive_int


def test_normalize_persian_and_arabic_indic_digits():
    assert normalize_digits("۱۴۰۵ ١٢٣") == "1405 123"


def test_parse_positive_int_accepts_localized_digits_and_separators():
    assert parse_positive_int("۱٬۲۳۴") == 1234
    assert parse_positive_int("1,234") == 1234


def test_parse_positive_int_rejects_zero_and_non_digits():
    with pytest.raises(ValidationError):
        parse_positive_int("۰")
    with pytest.raises(ValidationError):
        parse_positive_int("12abc")


def test_normalize_name_collapses_whitespace():
    assert normalize_name("  دهم   تجربی  ") == "دهم تجربی"


def test_normalize_name_rejects_control_only_content():
    with pytest.raises(ValidationError):
        normalize_name("\n\t")
    with pytest.raises(ValidationError):
        normalize_name("   ")
