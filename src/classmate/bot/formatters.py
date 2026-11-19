import logging
from html import escape

from aiogram import Bot

from classmate.bot.keyboards import approval_keyboard
from classmate.db.models import ClassGroup
from classmate.domain.dates import jalali_date, jalali_datetime
from classmate.domain.enums import ENTITY_LABELS

logger = logging.getLogger(__name__)


async def _pending_message(
    bot: Bot, class_group: ClassGroup, entity_type: str, record_id: int,
    title: str, details: str, creator_name: str,
) -> None:
    body = (
        f"🕓 <b>درخواست جدید برای بررسی</b>\n"
        f"نوع: {escape(ENTITY_LABELS.get(entity_type, 'اطلاعات'))}\n"
        f"عنوان: {escape(title)}\n"
        f"جزئیات: {escape(details)}\n"
        f"ثبت‌کننده: {escape(creator_name)}\n\n"
        "این اطلاعات تا زمان تأیید، قطعی نیست."
    )
    if class_group.group_chat_id:
        try:
            await bot.send_message(
                class_group.group_chat_id,
                body,
                reply_markup=approval_keyboard(entity_type, record_id),
            )
        except Exception:
            logger.exception("Could not post pending record to class group %s", class_group.id)


def _format_homework(rows) -> str:
    if not rows:
        return "هنوز تکلیف تأییدشده‌ای ثبت نشده است."
    lines = [
        "📚 <b>تکالیف تأییدشده</b>",
        "<i>حداکثر ۳۰ تکلیف در این فهرست نمایش داده می‌شود.</i>",
    ]
    for homework, subject in rows:
        due = jalali_date(homework.due_on) if homework.due_on else "نامشخص"
        lines.append(
            f"\n• <b>{escape(subject.name)}</b> — {escape(homework.title)}\n"
            f"  {escape(homework.description)}\n  موعد: {due}"
        )
    return "\n".join(lines)


def _format_exams(rows, timezone_name: str) -> str:
    if not rows:
        return "امتحان آینده‌ای ثبت نشده است."
    lines = ["📝 <b>امتحانات تأییدشده</b>"]
    for exam, subject in rows:
        lines.append(
            f"\n• <b>{escape(subject.name)}</b> — {escape(exam.title)}\n"
            f"  محدوده: {escape(exam.scope or 'اعلام نشده')}\n"
            f"  زمان: {jalali_datetime(exam.scheduled_at, timezone_name)}"
        )
    return "\n".join(lines)
