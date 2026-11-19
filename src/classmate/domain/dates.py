from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import jdatetime


_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(value: str) -> str:
    return value.translate(_DIGIT_MAP).strip()


def parse_school_date(value: str) -> date:
    """Accept ISO Gregorian dates or Jalali dates (1405/08/27)."""
    value = normalize_digits(value).replace("-", "/")
    parts = value.split("/")
    if len(parts) != 3:
        raise ValueError("تاریخ را به شکل ۱۴۰۵/۰۸/۲۷ یا 2026/11/18 وارد کنید.")
    year, month, day = (int(part) for part in parts)
    if year >= 1700:
        return date(year, month, day)
    return jdatetime.date(year, month, day).togregorian()


def parse_school_datetime(value: str, timezone_name: str) -> datetime:
    """Parse Jalali/Gregorian date plus HH:MM and return a timezone-aware datetime."""
    normalized = normalize_digits(value)
    try:
        date_part, time_part = normalized.rsplit(" ", 1)
        hour, minute = (int(part) for part in time_part.split(":"))
        parsed_date = parse_school_date(date_part)
        return datetime.combine(
            parsed_date, time(hour, minute), tzinfo=ZoneInfo(timezone_name)
        )
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "زمان را به شکل ۱۴۰۵/۰۸/۲۷ ۰۹:۰۰ وارد کنید."
        ) from exc


def jalali_date(value: date) -> str:
    return jdatetime.date.fromgregorian(date=value).strftime("%Y/%m/%d")


def jalali_datetime(value: datetime, timezone_name: str) -> str:
    local_value = value.astimezone(ZoneInfo(timezone_name))
    return jdatetime.datetime.fromgregorian(datetime=local_value).strftime("%Y/%m/%d %H:%M")


def persian_weekday(value: date) -> int:
    """Return 0=Saturday .. 6=Friday."""
    return (value.weekday() + 2) % 7


def next_timetable_occurrence(
    now: datetime,
    weekday: int,
    starts_at: time | None = None,
    ends_at: time | None = None,
    valid_from: date | None = None,
    valid_until: date | None = None,
) -> tuple[datetime, datetime | None] | None:
    """Find the next future class occurrence without mutating the search base date."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if weekday not in range(7):
        raise ValueError("weekday must be between 0 (Saturday) and 6 (Friday)")
    search_start = max(now.date(), valid_from or date.min)
    if valid_until is not None and search_start > valid_until:
        return None
    for offset in range(14):
        lesson_date = search_start + timedelta(days=offset)
        if valid_until is not None and lesson_date > valid_until:
            return None
        if persian_weekday(lesson_date) != weekday:
            continue
        lesson_start = datetime.combine(lesson_date, starts_at or time(8, 0), tzinfo=now.tzinfo)
        if lesson_start <= now:
            continue
        lesson_end = (
            datetime.combine(lesson_date, ends_at, tzinfo=now.tzinfo)
            if ends_at is not None else None
        )
        return lesson_start, lesson_end
    return None
