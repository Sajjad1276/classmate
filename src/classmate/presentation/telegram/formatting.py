from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import jdatetime


def html(value: str | None) -> str:
    return escape(value or "", quote=False)


def persian_date(value) -> str:
    return jdatetime.date.fromgregorian(date=value).strftime("%Y/%m/%d")


def persian_datetime(value: datetime, timezone_name: str) -> str:
    local = value.astimezone(ZoneInfo(timezone_name))
    return jdatetime.datetime.fromgregorian(datetime=local).strftime("%Y/%m/%d - %H:%M")
