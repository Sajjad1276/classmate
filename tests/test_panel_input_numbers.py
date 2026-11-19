from classmate.bot.panel_handlers import _parse_persian_int


def test_parse_ascii_digits():
    assert _parse_persian_int("123") == 123


def test_parse_persian_digits():
    assert _parse_persian_int("۱۲۳") == 123


def test_parse_arabic_indic_digits():
    assert _parse_persian_int("١٢٣") == 123


def test_parse_digits_with_thousands_separator_and_spaces():
    assert _parse_persian_int(" ۱٬۲۳۴ ") == 1234
