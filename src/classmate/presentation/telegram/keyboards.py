from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from classmate.db.models import ClassGroup, Subject


def main_keyboard(*, platform_admin: bool, school_admin: bool, class_manager: bool, approver: bool):
    rows = [
        [KeyboardButton(text="🏫 کلاس‌های من"), KeyboardButton(text="🔑 عضویت در کلاس")],
        [KeyboardButton(text="➕ ثبت اطلاعات")],
        [KeyboardButton(text="📚 تکالیف"), KeyboardButton(text="📝 امتحانات")],
        [KeyboardButton(text="📅 برنامه هفتگی"), KeyboardButton(text="🗒 امروز")],
        [KeyboardButton(text="🔔 یادآوری"), KeyboardButton(text="🔎 جست‌وجو")],
        [KeyboardButton(text="ℹ️ راهنما")],
    ]
    if approver:
        rows.append([KeyboardButton(text="🕓 صف بررسی")])
    if class_manager:
        rows.append([KeyboardButton(text="⚙️ مدیریت کلاس")])
    if school_admin:
        rows.append([KeyboardButton(text="🏢 مدیریت مدرسه")])
    if platform_admin:
        rows.append([KeyboardButton(text="🛡 مدیر کل")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="از منو استفاده کن",
    )


def inline_menu(
    rows: list[list[InlineKeyboardButton]], back: str = "v2:home"
) -> InlineKeyboardMarkup:
    rows = [*rows, [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back)]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def classes_menu(items: list[ClassGroup], action: str) -> InlineKeyboardMarkup:
    rows = [
        [_button(f"🏫 {item.name[:42]}", f"v2:class:{action}:{item.id}")]
        for item in items
    ]
    return inline_menu(rows)


def subjects_menu(items: list[Subject], action: str, class_id: int) -> InlineKeyboardMarkup:
    rows = [
        [_button(item.name[:50], f"v2:subject:{action}:{class_id}:{item.id}")]
        for item in items
    ]
    return inline_menu(rows, back=f"v2:class:open:{class_id}")


def record_types_menu(class_id: int) -> InlineKeyboardMarkup:
    return inline_menu(
        [
            [_button("📚 تکلیف", f"v2:record:homework:{class_id}")],
            [_button("📝 امتحان", f"v2:record:exam:{class_id}")],
            [_button("🗒 گزارش روزانه", f"v2:record:report:{class_id}")],
        ],
        back=f"v2:class:open:{class_id}",
    )


def approval_menu(class_id: int, record_type: str, record_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("✅ تأیید", f"v2:review:approve:{record_type}:{class_id}:{record_id}"),
                _button("❌ رد", f"v2:review:reject:{record_type}:{class_id}:{record_id}"),
            ]
        ]
    )


def reminder_menu(class_id: int, homework: bool, exams: bool) -> InlineKeyboardMarkup:
    return inline_menu(
        [
            [
                _button(
                    f"{'🟢' if homework else '⚫'} تکلیف",
                    f"v2:reminder:homework:{class_id}:{int(not homework)}",
                )
            ],
            [
                _button(
                    f"{'🟢' if exams else '⚫'} امتحان",
                    f"v2:reminder:exams:{class_id}:{int(not exams)}",
                )
            ],
        ],
        back=f"v2:class:open:{class_id}",
    )


def _button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)
