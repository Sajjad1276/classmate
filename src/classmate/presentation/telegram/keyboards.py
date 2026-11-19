from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from classmate.db.models import ClassGroup, Subject


def button(text: str, callback: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback)


def menu(rows: list[list[InlineKeyboardButton]], back: str | None = "v2:home"):
    keyboard = list(rows)
    if back is not None:
        keyboard.append([button("⬅️ بازگشت", back)])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def main_keyboard(*, platform_admin: bool, school_admin: bool, class_manager: bool, approver: bool):
    rows = [
        [button("🏫 کلاس‌های من", "v2:classes:open")],
        [button("➕ ثبت اطلاعات", "v2:classes:new")],
        [button("📚 تکالیف", "v2:classes:homework"), button("📝 امتحانات", "v2:classes:exams")],
        [button("📅 برنامه هفتگی", "v2:classes:week"), button("🗒 امروز", "v2:classes:today")],
        [button("🔔 یادآوری", "v2:classes:reminders"), button("🔎 جست‌وجو", "v2:classes:search")],
    ]
    if approver:
        rows.append([button("🕓 صف بررسی", "v2:classes:pending")])
    if class_manager:
        rows.append([button("⚙️ مدیریت کلاس", "v2:classes:manage")])
    if school_admin:
        rows.append([button("🏢 مدیریت مدرسه", "v2:schools:manage")])
    if platform_admin:
        rows.append([button("🛡 پنل مدیر کل", "v2:platform")])
    return menu(rows, back=None)


def classes_menu(items: list[ClassGroup], action: str):
    return menu([
        [button(f"🏫 {item.name[:42]}", f"v2:class:{action}:{item.id}")]
        for item in items
    ])


def class_dashboard(
    class_id: int,
    *,
    can_review: bool,
    can_manage: bool,
    group_mode: bool = False,
):
    rows = [
        [button("➕ ثبت تکلیف، امتحان یا گزارش", f"v2:new:{class_id}")],
        [button("📚 تکالیف", f"v2:action:homework:{class_id}"),
         button("📝 امتحانات", f"v2:action:exams:{class_id}")],
        [button("📅 برنامه هفتگی", f"v2:action:week:{class_id}"),
         button("🗒 خلاصه امروز", f"v2:action:today:{class_id}")],
    ]
    if not group_mode:
        rows.append([
            button("🔔 یادآوری‌های من", f"v2:action:reminders:{class_id}"),
            button("🔎 جست‌وجو", f"v2:action:search:{class_id}"),
        ])
    ]
    if can_review:
        rows.append([button("🕓 صف بررسی", f"v2:action:pending:{class_id}")])
    if can_manage and not group_mode:
        rows.append([button("⚙️ مدیریت کلاس", f"v2:action:manage:{class_id}")])
    return menu(rows, back=None if group_mode else "v2:home")


def record_types(class_id: int):
    return menu([
        [button("📚 تکلیف", f"v2:record:homework:{class_id}")],
        [button("📝 امتحان", f"v2:record:exam:{class_id}")],
        [button("🗒 گزارش روزانه", f"v2:record:report:{class_id}")],
    ], back=f"v2:class:open:{class_id}")


def subject_picker(subjects: list[Subject], record_type: str, class_id: int):
    return menu([
        [button(item.name[:50], f"v2:subject:{record_type}:{class_id}:{item.id}")]
        for item in subjects
    ], back=f"v2:class:open:{class_id}")


def approval_buttons(class_id: int, record_type: str, record_id: int):
    return [
        button("✅ تأیید", f"v2:review:approve:{record_type}:{class_id}:{record_id}"),
        button("❌ رد", f"v2:review:reject:{record_type}:{class_id}:{record_id}"),
    ]


def reminder_preferences(class_id: int, homework: bool, exams: bool):
    return menu([
        [button(f"{'🟢' if homework else '⚫'} یادآوری تکلیف", f"v2:reminder:homework:{class_id}:{int(not homework)}")],
        [button(f"{'🟢' if exams else '⚫'} یادآوری امتحان", f"v2:reminder:exams:{class_id}:{int(not exams)}")],
    ], back=f"v2:class:open:{class_id}")


def cancel_form():
    return InlineKeyboardMarkup(
        inline_keyboard=[[button("❌ لغو فرم", "v2:form:cancel")]]
    )


def form_navigation(back: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[[button("⬅️ بازگشت", back), button("❌ لغو فرم", "v2:form:cancel")]]
    )
