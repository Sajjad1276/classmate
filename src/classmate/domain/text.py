import re
import unicodedata

from classmate.domain.errors import ValidationError


_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def normalize_digits(value: str) -> str:
    return value.translate(_DIGITS)


def normalize_name(value: str, *, minimum: int = 2, maximum: int = 120) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = " ".join(normalized.split()).strip()
    normalized = re.sub(r"[\x00-\x1f\x7f]", "", normalized)
    if not minimum <= len(normalized) <= maximum:
        raise ValidationError(f"نام باید بین {minimum} تا {maximum} نویسه باشد.")
    return normalized


def normalize_body(value: str, *, minimum: int = 2, maximum: int = 4000) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip()
    if len(normalized) < minimum:
        raise ValidationError("متن واردشده کوتاه است.")
    if len(normalized) > maximum:
        raise ValidationError(f"متن نباید بیشتر از {maximum} نویسه باشد.")
    return normalized


def parse_positive_int(value: str, *, maximum: int = 2_147_483_647) -> int:
    normalized = normalize_digits(value).replace(",", "").replace("٬", "").strip()
    if not normalized.isascii() or not normalized.isdigit():
        raise ValidationError("یک عدد معتبر وارد کن.")
    number = int(normalized)
    if not 1 <= number <= maximum:
        raise ValidationError("عدد واردشده خارج از محدوده است.")
    return number
