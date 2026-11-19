from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

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



def main_reply_keyboard(
    *,
    is_platform_admin: bool = False,
    is_school_admin: bool = False,
    is_class_manager: bool = False,
    is_approver: bool = False,
) -> ReplyKeyboardMarkup:
    """Persistent, role-aware private-chat home keyboard."""
    rows = [
        [KeyboardButton(text="🏫 کلاس‌های من"), KeyboardButton(text="🔑 عضویت در کلاس")],
        [KeyboardButton(text="➕ ثبت اطلاعات")],
        [KeyboardButton(text="📚 تکالیف"), KeyboardButton(text="📝 امتحانات")],
        [KeyboardButton(text="📅 برنامه هفتگی"), KeyboardButton(text="🗒 گزارش امروز")],
        [KeyboardButton(text="🔎 جست‌وجو"), KeyboardButton(text="🔔 یادآوری")],
        [KeyboardButton(text="ℹ️ راهنمای استفاده")],
    ]
    if is_approver:
        rows.append([KeyboardButton(text="🕓 صف بررسی")])
    if is_class_manager:
        rows.append([KeyboardButton(text="⚙️ مدیریت کلاس")])
    if is_school_admin:
        rows.append([KeyboardButton(text="🏢 مدیریت مدرسه")])
    if is_platform_admin:
        rows.append([KeyboardButton(text="🛡 پنل مدیر کل")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="از دکمه‌های منو استفاده کن",
    )


def wizard_cancel_keyboard() -> ReplyKeyboardMarkup:
    """One navigation control: leave the wizard and restore the main menu."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ بازگشت")]],
        resize_keyboard=True,
        is_persistent=True,
    )



def group_menu_keyboard(*, can_review: bool = False) -> InlineKeyboardMarkup:
    """Compact, class-scoped menu for a connected Telegram group."""
    rows = [
        [
            InlineKeyboardButton(text="📚 تکالیف", callback_data="group:homework"),
            InlineKeyboardButton(text="📝 امتحانات", callback_data="group:exams"),
        ],
        [
            InlineKeyboardButton(text="📅 برنامه هفتگی", callback_data="group:week"),
            InlineKeyboardButton(text="🗒 امروز", callback_data="group:today"),
        ],
        [
            InlineKeyboardButton(text="➕ ثبت تکلیف", callback_data="new:homework"),
            InlineKeyboardButton(text="➕ ثبت امتحان", callback_data="new:exam"),
        ],
        [InlineKeyboardButton(text="🗒 ثبت گزارش روزانه", callback_data="new:report")],
    ]
    if can_review:
        rows.append(
            [InlineKeyboardButton(text="🕓 صف بررسی", callback_data="group:pending")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
