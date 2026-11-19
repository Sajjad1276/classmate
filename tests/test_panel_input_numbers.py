import pytest

from classmate.bot.panel_handlers import _parent_for_form, _parse_persian_int


def test_parse_ascii_digits():
    assert _parse_persian_int("123") == 123


def test_parse_persian_digits():
    assert _parse_persian_int("۱۲۳") == 123


def test_parse_arabic_indic_digits():
    assert _parse_persian_int("١٢٣") == 123


def test_parse_digits_with_thousands_separator_and_spaces():
    assert _parse_persian_int(" ۱٬۲۳۴ ") == 1234

def test_parse_number_with_regular_and_nonbreaking_spaces():
    assert _parse_persian_int("۱ ۲۳۴") == 1234
    assert _parse_persian_int("1\u00a0234") == 1234


def test_parse_empty_integer_is_rejected():
    with pytest.raises(ValueError):
        _parse_persian_int("   ")


def test_year_and_class_forms_return_to_selected_school():
    assert _parent_for_form("year_new", None, 17) == "ui:school_manage:17"
    assert _parent_for_form("class_new", None, 21) == "ui:school_manage:21"


def test_school_admin_assignment_returns_to_platform_panel():
    assert _parent_for_form("school_admin_add", None, 17) == "ui:platform"


def test_class_form_returns_to_its_management_panel():
    assert _parent_for_form("subject_add", 42) == "ui:manage:42"
