from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from classmate.db.models import ClassGroup, Subject


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ ثبت اطلاعات", callback_data="menu:new"),
                InlineKeyboardButton(text="🏫 کلاس‌های من", callback_data="menu:classes"),
            ],
            [
                InlineKeyboardButton(text="📚 تکالیف", callback_data="menu:homework"),
                InlineKeyboardButton(text="📝 امتحانات", callback_data="menu:exams"),
            ],
            [
                InlineKeyboardButton(text="📅 برنامه هفتگی", callback_data="menu:week"),
                InlineKeyboardButton(text="🔎 راهنمای جست‌وجو", callback_data="menu:search"),
            ],
            [InlineKeyboardButton(text="🔔 تنظیم یادآوری", callback_data="menu:reminders")],
        ]
    )


def new_record_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗒 گزارش روزانه", callback_data="new:report"),
                InlineKeyboardButton(text="📚 تکلیف", callback_data="new:homework"),
            ],
            [InlineKeyboardButton(text="📝 امتحان", callback_data="new:exam")],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="menu:home")],
        ]
    )


def class_picker(classes: list[ClassGroup]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=item.name[:60], callback_data=f"wizard:class:{item.id}")]
            for item in classes
        ]
    )


def subject_picker(subjects: list[Subject]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=item.name[:60], callback_data=f"wizard:subject:{item.id}")]
            for item in subjects
        ]
    )


def approval_keyboard(entity_type: str, entity_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید",
                    callback_data=f"approval:approve:{entity_type}:{entity_id}",
                ),
                InlineKeyboardButton(
                    text="❌ رد",
                    callback_data=f"approval:reject:{entity_type}:{entity_id}",
                ),
            ]
        ]
    )
