from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from sqlalchemy import select

from classmate.bot.handlers import (
    WEEKDAYS,
    _classes_for_user,
    _ensure_group_member,
    _format_exams,
    _format_homework,
    _parse_pipe_args,
    _user,
)
from classmate.bot.keyboards import main_reply_keyboard
from classmate.bot.states import PanelInput
from classmate.core.config import get_settings
from classmate.db.models import (
    AcademicYear,
    ClassGroup,
    ClassMembership,
    NotificationPreference,
    School,
    SchoolMembership,
    TimetableEntry,
    User,
)
from classmate.db.session import SessionFactory
from classmate.domain.dates import jalali_date, jalali_datetime
from classmate.services.permissions import (
    can_approve,
    get_class_with_school,
    is_school_admin,
    make_code,
    hash_code,
    user_can_access_class,
)
from classmate.services.records import (
    add_subject,
    approve_record,
    audit,
    get_or_create_next_lesson,
    get_subject,
    list_exams,
    list_homework,
    list_homework_for_lesson,
    list_pending_records,
    list_daily_summary,
    list_current_timetable,
    get_record_submitter_telegram_id,
    normalize_subject_name,
    reject_record,
)

logger = logging.getLogger(__name__)
router = Router(name="classmate_panels")

MAIN_BUTTONS = {
    "🏫 کلاس‌های من",
    "🔑 عضویت در کلاس",
    "➕ ثبت اطلاعات",
    "📚 تکالیف",
    "📝 امتحانات",
    "📅 برنامه هفتگی",
    "🗒 گزارش امروز",
    "🔎 جست‌وجو",
    "🔔 یادآوری",
    "ℹ️ راهنمای استفاده",
    "🕓 صف بررسی",
    "⚙️ مدیریت کلاس",
    "🏢 مدیریت مدرسه",
    "🛡 پنل مدیر کل",
    "⬅️ بازگشت",
}

ACTION_LABELS = {
    "homework": "تکالیف تأییدشده",
    "exams": "امتحانات تأییدشده",
    "today": "گزارش روزانه",
    "week": "برنامه هفتگی",
    "ask": "جست‌وجو در اطلاعات کلاس",
    "reminders": "یادآوری‌ها",
    "pending": "صف بررسی",
    "manage": "مدیریت کلاس",
}


def _markup(
    rows: list[list[InlineKeyboardButton]],
    back: str = "ui:close",
) -> InlineKeyboardMarkup:
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _should_create_panel_message(
    panel_message_id: int | None,
    reply_keyboard_visible: bool,
) -> bool:
    """A visible reply keyboard must be removed on a fresh message, not an edit."""
    return panel_message_id is None or reply_keyboard_visible


async def _render_from_message(
    message: Message,
    state: FSMContext,
    bot: Bot,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit the existing panel message; create one only when no panel exists."""
    data = await state.get_data()
    panel_id = data.get("panel_message_id")
    keyboard_visible = data.get("reply_keyboard_visible", False)
    create_new_panel = _should_create_panel_message(panel_id, keyboard_visible)
    if panel_id and not create_new_panel:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=message.chat.id,
                message_id=panel_id,
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.info("Could not edit previous panel message: %s", exc)
    if panel_id and create_new_panel:
        try:
            await bot.delete_message(message.chat.id, panel_id)
        except TelegramBadRequest:
            pass
    sent = await bot.send_message(
        message.chat.id,
        text,
        reply_markup=ReplyKeyboardRemove(),
    )
    if keyboard is not None:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=sent.message_id,
            reply_markup=keyboard,
        )
    await state.update_data(
        panel_message_id=sent.message_id,
        reply_keyboard_visible=False,
    )


async def _render_from_callback(
    callback: CallbackQuery,
    state: FSMContext,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await state.update_data(
            panel_message_id=callback.message.message_id,
            reply_keyboard_visible=False,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            sent = await callback.message.answer(text, reply_markup=keyboard)
            await state.update_data(
                panel_message_id=sent.message_id,
                reply_keyboard_visible=False,
            )
    await callback.answer()


async def _close_panel(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    await state.clear()
    await state.update_data(reply_keyboard_visible=True)
    _, roles = await _current_user(callback)
    await callback.bot.send_message(
        callback.message.chat.id,
        "منوی اصلی ClassMate",
        reply_markup=main_reply_keyboard(
            is_platform_admin=roles["platform"],
            is_school_admin=roles["school"],
            is_class_manager=roles["manager"],
            is_approver=roles["approver"],
        ),
    )
    await callback.answer()


async def _current_user(message_or_callback, private: bool = True):
    tg_user = (
        message_or_callback.from_user
        if isinstance(message_or_callback, Message)
        else message_or_callback.from_user
    )
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, tg_user, private=private)
            school_admin = user.is_platform_admin or (
                await session.scalar(
                    select(SchoolMembership.id).where(
                        SchoolMembership.user_id == user.id,
                        SchoolMembership.role == "school_admin",
                        SchoolMembership.is_active.is_(True),
                    )
                )
            ) is not None
            memberships = list(
                (
                    await session.scalars(
                        select(ClassMembership.role).where(
                            ClassMembership.user_id == user.id,
                            ClassMembership.is_active.is_(True),
                        )
                    )
                ).all()
            )
            roles = set(memberships)
            class_manager = "class_manager" in roles
            approver = school_admin or class_manager or "trusted_approver" in roles
            return user, {
                "platform": bool(user.is_platform_admin),
                "school": bool(school_admin),
                "manager": class_manager,
                "approver": approver,
            }


async def _get_classes(tg_user):
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, tg_user, private=True)
            classes = await _classes_for_user(session, user)
            return [(item.id, item.name) for item in classes]


async def _show_classes(
    callback: CallbackQuery,
    state: FSMContext,
    tg_user,
    action: str = "open",
) -> None:
    classes = await _get_classes(tg_user)
    if not classes:
        await _render_from_callback(
            callback,
            state,
            "🏫 هنوز کلاسی به حساب تو متصل نشده است. از مدیر کلاس کد دعوت بگیر و در بخش عضویت وارد کن.",
            _markup([], "ui:close"),
        )
        return
    rows = [
        [_button(f"🏫 {name[:48]}", f"ui:class:{class_id}")]
        for class_id, name in classes
    ]
    await state.update_data(pending_class_action=action)
    await _render_from_callback(
        callback,
        state,
        "کلاس موردنظر را انتخاب کن:",
        _markup(rows),
    )


async def _show_class_selector_from_message(
    message: Message,
    state: FSMContext,
    bot: Bot,
    tg_user,
    action: str,
) -> None:
    classes = await _get_classes(tg_user)
    if action in {"pending", "manage"}:
        eligible = []
        for candidate_id, candidate_name in classes:
            context = await _class_context(candidate_id, tg_user)
            if not context:
                continue
            allowed = context["is_approver"] if action == "pending" else (
                context["is_manager"] or context["is_school_admin"] or context["is_platform_admin"]
            )
            if allowed:
                eligible.append((candidate_id, candidate_name))
        classes = eligible
    if not classes:
        notice = (
            "در هیچ‌کدام از کلاس‌های قابل‌دسترسی، مجوز بررسی نداری."
            if action == "pending"
            else "کلاسی با دسترسی مدیریت برای این حساب پیدا نشد."
            if action == "manage"
            else "🏫 هنوز کلاسی به حساب تو متصل نشده است. از مدیر کلاس کد دعوت بگیر."
        )
        await _render_from_message(
            message,
            state,
            bot,
            notice,
            _markup([]),
        )
        return
    if len(classes) == 1:
        await _show_action(message, state, bot, tg_user, action, classes[0][0])
        return
    rows = [
        [_button(f"🏫 {name[:48]}", f"ui:action:{action}:{class_id}")]
        for class_id, name in classes
    ]
    await _render_from_message(
        message,
        state,
        bot,
        "برای ادامه، کلاس موردنظر را انتخاب کن:",
        _markup(rows),
    )


async def _class_context(class_id: int, tg_user):
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, tg_user, private=True)
            context = await get_class_with_school(session, class_id)
            if not context or not await user_can_access_class(session, user, class_id):
                return None
            class_group, year, school = context
            membership = await session.scalar(
                select(ClassMembership).where(
                    ClassMembership.class_id == class_id,
                    ClassMembership.user_id == user.id,
                    ClassMembership.is_active.is_(True),
                )
            )
            is_manager = bool(membership and membership.role == "class_manager")
            is_approver = await can_approve(session, user, class_id)
            is_school = await is_school_admin(session, user, school.id)
            return {
                "user": user,
                "class": class_group,
                "year": year,
                "school": school,
                "is_manager": is_manager,
                "is_approver": is_approver,
                "is_school_admin": is_school,
                "is_platform_admin": user.is_platform_admin,
                "role": membership.role if membership else "school_admin",
            }


async def _show_class_panel(
    callback: CallbackQuery,
    state: FSMContext,
    class_id: int,
) -> None:
    context = await _class_context(class_id, callback.from_user)
    if not context:
        await callback.answer("به این کلاس دسترسی نداری.", show_alert=True)
        return
    item = context["class"]
    school = context["school"]
    year = context["year"]
    title = (
        f"🏫 <b>{escape(item.name)}</b>\n"
        f"مدرسه: {escape(school.name)}\n"
        f"پایه: {escape(item.grade)} | سال تحصیلی: {escape(year.name)}"
    )
    rows = [
        [
            _button("📚 تکالیف", f"ui:homework:{class_id}"),
            _button("📝 امتحانات", f"ui:exams:{class_id}"),
        ],
        [
            _button("📅 برنامه هفتگی", f"ui:week:{class_id}"),
            _button("🗒 گزارش امروز", f"ui:today:{class_id}"),
        ],
        [
            _button("🔎 جست‌وجو", f"ui:ask:{class_id}"),
            _button("🔔 یادآوری", f"ui:reminders:{class_id}"),
        ],
    ]
    if context["is_approver"]:
        rows.append([_button("🕓 صف بررسی", f"ui:pending:{class_id}")])
    if context["is_manager"] or context["is_school_admin"] or context["is_platform_admin"]:
        rows.append([_button("⚙️ مدیریت این کلاس", f"ui:manage:{class_id}")])
    await _render_from_callback(callback, state, title, _markup(rows, "ui:classes"))


async def _show_action(
    message: Message,
    state: FSMContext,
    bot: Bot,
    tg_user,
    action: str,
    class_id: int,
) -> None:
    context = await _class_context(class_id, tg_user)
    if not context:
        await _render_from_message(message, state, bot, "به این کلاس دسترسی نداری.", _markup([]))
        return
    class_group, school = context["class"], context["school"]
    title = f"{ACTION_LABELS.get(action, 'بخش کلاس')} — {escape(class_group.name)}"
    if action == "homework":
        async with SessionFactory() as session:
            async with session.begin():
                rows = await list_homework(session, class_id)
        body = _format_homework(rows)
    elif action == "exams":
        async with SessionFactory() as session:
            async with session.begin():
                rows = await list_exams(session, class_id)
        body = _format_exams(rows, school.timezone)
    elif action == "today":
        async with SessionFactory() as session:
            async with session.begin():
                today = datetime.now(ZoneInfo(school.timezone)).date()
                reports, homework_rows, exam_rows = await list_daily_summary(
                    session, class_id, today, school.timezone
                )
        parts = [f"🗓 <b>خلاصه امروز — {jalali_date(today)}</b>"]
        if reports:
            parts.extend(
                f"\n🗒 <b>{escape(report.title or 'گزارش روزانه')}</b>\n{escape(report.body)}"
                for report in reports
            )
        else:
            parts.append("\n🗒 گزارش تأییدشده‌ای برای امروز ثبت نشده است.")
        parts.append("\n" + _format_homework(homework_rows))
        parts.append("\n" + _format_exams(exam_rows, school.timezone))
        body = "\n".join(parts)
    elif action == "week":
        async with SessionFactory() as session:
            async with session.begin():
                today = datetime.now(ZoneInfo(school.timezone)).date()
                entries = await list_current_timetable(session, class_id, today)
        body = (
            "برنامه هفتگی هنوز برای این کلاس ثبت نشده است."
            if not entries
            else "📅 <b>برنامه هفتگی</b>\n"
            + "\n".join(
                f"{WEEKDAYS[entry.weekday]} — زنگ {entry.period}"
                + (f" ({entry.starts_at.strftime('%H:%M')})" if entry.starts_at else "")
                + f": {escape(subject.name)}"
                for entry, subject in entries
            )
        )
    elif action == "reminders":
        async with SessionFactory() as session:
            async with session.begin():
                user = await _user(session, tg_user, private=True)
                pref = await session.scalar(
                    select(NotificationPreference).where(
                        NotificationPreference.user_id == user.id,
                        NotificationPreference.class_id == class_id,
                    )
                )
        homework_enabled = pref.homework_enabled if pref else True
        exams_enabled = pref.exams_enabled if pref else True
        body = (
            f"🔔 <b>تنظیم یادآوری‌های خصوصی</b>\n"
            f"تکالیف: {'فعال ✅' if homework_enabled else 'غیرفعال ⛔'}\n"
            f"امتحانات: {'فعال ✅' if exams_enabled else 'غیرفعال ⛔'}"
        )
        rows = [
            [
                _button(
                    "خاموش‌کردن تکالیف" if homework_enabled else "فعال‌کردن تکالیف",
                    f"ui:reminder_toggle:{class_id}:homework:{'off' if homework_enabled else 'on'}",
                )
            ],
            [
                _button(
                    "خاموش‌کردن امتحانات" if exams_enabled else "فعال‌کردن امتحانات",
                    f"ui:reminder_toggle:{class_id}:exams:{'off' if exams_enabled else 'on'}",
                )
            ],
        ]
        await _render_from_message(
            message, state, bot, f"{title}\n\n{body}", _markup(rows, f"ui:class:{class_id}")
        )
        return
    elif action == "pending":
        await _show_pending_from_message(message, state, bot, class_id, tg_user)
        return
    elif action == "manage":
        await _show_management_from_message(message, state, bot, class_id, tg_user)
        return
    elif action == "ask":
        await state.set_state(PanelInput.input)
        await state.update_data(panel_form="search", panel_class_id=class_id)
        await _render_from_message(
            message,
            state,
            bot,
            "🔎 پرسشت را درباره تکلیف یا امتحان بنویس. جست‌وجو بر اساس اطلاعات تأییدشده است.",
            _markup([], "ui:cancel_input"),
        )
        return
    else:
        body = "این بخش هنوز پیاده‌سازی نشده است."
        rows = []
    back = f"ui:class:{class_id}"
    await _render_from_message(message, state, bot, f"{title}\n\n{body}", _markup([], back))


async def _show_pending_from_message(
    message: Message,
    state: FSMContext,
    bot: Bot,
    class_id: int,
    tg_user,
) -> None:
    context = await _class_context(class_id, tg_user)
    if not context or not context["is_approver"]:
        await _render_from_message(message, state, bot, "دسترسی بررسی این کلاس را نداری.", _markup([]))
        return
    async with SessionFactory() as session:
        async with session.begin():
            pending = await list_pending_records(session, class_id)
    if not pending:
        await _render_from_message(
            message, state, bot, "🕓 موردی در انتظار بررسی نیست.", _markup([], f"ui:class:{class_id}")
        )
        return
    rows = []
    lines = ["🕓 <b>درخواست‌های در انتظار بررسی</b>"]
    for kind, record in pending[:8]:
        title = getattr(record, "title", "گزارش روزانه")
        details = getattr(record, "description", getattr(record, "body", getattr(record, "scope", "")))
        lines.append(f"\n<b>{escape(kind)} #{record.id} — {escape(title)}</b>\n{escape(details[:220])}")
        rows.append(
            [
                _button("✅ تأیید", f"ui:review:approve:{kind}:{record.id}:{class_id}"),
                _button("❌ رد", f"ui:review:reject:{kind}:{record.id}:{class_id}"),
            ]
        )
    if len(pending) > 8:
        lines.append("\nفقط ۸ مورد نخست نمایش داده شده است.")
    await _render_from_message(
        message, state, bot, "\n".join(lines), _markup(rows, f"ui:class:{class_id}")
    )


async def _show_management_from_message(
    message: Message,
    state: FSMContext,
    bot: Bot,
    class_id: int,
    tg_user,
) -> None:
    context = await _class_context(class_id, tg_user)
    if not context or not (
        context["is_manager"] or context["is_school_admin"] or context["is_platform_admin"]
    ):
        await _render_from_message(message, state, bot, "دسترسی مدیریت این کلاس را نداری.", _markup([]))
        return
    rows = [
        [_button("🔐 ساخت کد اتصال و عضویت", f"ui:form:class_code:{class_id}")],
        [_button("👤 افزودن فرد مورد اعتماد", f"ui:form:approver_add:{class_id}")],
        [_button("📘 افزودن درس", f"ui:form:subject_add:{class_id}")],
        [_button("🗓 تنظیم زنگ برنامه", f"ui:form:timetable_set:{class_id}")],
    ]
    await _render_from_message(
        message,
        state,
        bot,
        f"⚙️ <b>مدیریت کلاس {escape(context['class'].name)}</b>\n"
        "عملیات مدیریتی فقط برای نقش‌های مجاز فعال هستند.",
        _markup(rows, f"ui:class:{class_id}"),
    )


async def _show_platform_panel(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    user, roles = await _current_user(callback)
    if not roles["platform"]:
        await callback.answer("این پنل فقط برای مدیر کل است.", show_alert=True)
        return
    rows = [
        [_button("🏫 ایجاد مدرسه و سال تحصیلی", "ui:form:school_new")],
        [_button("👤 تعیین مدیر مدرسه", "ui:school_admin_schools")],
        [_button("📚 مدیریت مدرسه‌ها و کلاس‌ها", "ui:school")],
    ]
    await _render_from_callback(
        callback,
        state,
        "🛡 <b>پنل مدیر کل ClassMate</b>\n"
        f"حساب مدیر کل فعال است. شناسه تلگرام: <code>{callback.from_user.id}</code>\n"
        "از این بخش می‌توانی ساختار مدرسه‌ها و مدیران را مدیریت کنی.",
        _markup(rows),
    )


async def _admin_schools(tg_user) -> list[tuple[int, str]]:
    """Return only active schools the current account is allowed to administer."""
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, tg_user, private=True)
            query = select(School).where(School.is_active.is_(True))
            if not user.is_platform_admin:
                query = query.join(
                    SchoolMembership,
                    SchoolMembership.school_id == School.id,
                ).where(
                    SchoolMembership.user_id == user.id,
                    SchoolMembership.role == "school_admin",
                    SchoolMembership.is_active.is_(True),
                )
            schools = (await session.scalars(query.order_by(School.name))).all()
            return [(school.id, school.name) for school in schools]


async def _show_school_panel(callback: CallbackQuery, state: FSMContext) -> None:
    _, roles = await _current_user(callback)
    if not roles["school"]:
        await callback.answer("این بخش فقط برای مدیر مدرسه است.", show_alert=True)
        return
    schools = await _admin_schools(callback.from_user)
    rows = [
        [_button(f"🏫 {name[:42]}", f"ui:school_manage:{school_id}")]
        for school_id, name in schools
    ]
    body = (
        "🏢 <b>مدیریت مدرسه</b>\nمدرسه‌ای را برای مدیریت انتخاب کن."
        if schools
        else "برای این حساب مدرسه فعالی جهت مدیریت ثبت نشده است."
    )
    await _render_from_callback(callback, state, body, _markup(rows, "ui:close"))


async def _show_school_management(
    callback: CallbackQuery,
    state: FSMContext,
    school_id: int,
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            school = await session.get(School, school_id)
            if not school or not school.is_active or not await is_school_admin(session, user, school_id):
                await callback.answer("به مدیریت این مدرسه دسترسی نداری.", show_alert=True)
                return
            school_name = school.name
    rows = [
        [_button("📅 ایجاد سال تحصیلی", f"ui:form:year_new:{school_id}")],
        [_button("🏫 ایجاد کلاس", f"ui:form:class_new:{school_id}")],
        [_button("👤 تعیین مدیر کلاس", f"ui:school_classes:{school_id}")],
        [_button("📚 مشاهده کلاس‌های مدرسه", f"ui:school_classes_view:{school_id}")],
    ]
    await _render_from_callback(
        callback,
        state,
        f"🏢 <b>{escape(school_name)}</b>\nعملیات مدیریتی این مدرسه را انتخاب کن.",
        _markup(rows, "ui:school"),
    )


async def _show_form(
    callback: CallbackQuery,
    state: FSMContext,
    form_name: str,
    class_id: int | None = None,
    school_id: int | None = None,
) -> None:
    user, roles = await _current_user(callback)
    prompts = {
        "join": "کد دعوت کلاس را وارد کن.",
        "school_new": "نام مدرسه را وارد کن.",
        "school_admin_add": "شناسه عددی تلگرام کاربر را وارد کن.",
        "year_new": "نام سال تحصیلی را وارد کن.\nنمونه: ۱۴۰۶-۱۴۰۷",
        "class_new": "پایه | شعبه | نام کلاس\nبرای نداشتن شعبه، - وارد کن. نمونه: دهم | الف | دهم تجربی ۱",
        "class_manager_add": "شناسه عددی تلگرام مدیر جدید را وارد کن.",
        "class_code": "کدهای جدید برای کلاس انتخاب‌شده ساخته می‌شود.",
        "approver_add": "شناسه عددی تلگرام فرد مورد اعتماد را وارد کن.",
        "subject_add": "نام درس را وارد کن.",
        "timetable_set": (
            "روز | زنگ | نام درس | ساعت شروع-پایان\n"
            "نمونه: شنبه | ۱ | ریاضی | 08:00-08:45\n"
            "برای ساعت نامشخص، - وارد کن."
        ),
    }
    permitted = {
        "join": True,
        "school_new": roles["platform"],
        "school_admin_add": roles["platform"],
        "year_new": roles["school"],
        "class_new": roles["school"],
        "class_manager_add": roles["school"],
    }
    if form_name in {"year_new", "class_new"}:
        async with SessionFactory() as session:
            async with session.begin():
                actor = await _user(session, callback.from_user, private=True)
                school = await session.get(School, school_id) if school_id else None
                permitted[form_name] = bool(
                    school and school.is_active
                    and await is_school_admin(session, actor, school_id)
                )
    if form_name == "school_admin_add":
        async with SessionFactory() as session:
            async with session.begin():
                actor = await _user(session, callback.from_user, private=True)
                school = await session.get(School, school_id) if school_id else None
                permitted[form_name] = bool(
                    actor.is_platform_admin and school and school.is_active
                )
    if form_name == "class_manager_add" and class_id is not None:
        context = await _class_context(class_id, callback.from_user)
        permitted[form_name] = bool(context and context["is_school_admin"])
    if form_name in {"class_code", "approver_add", "subject_add", "timetable_set"}:
        context = await _class_context(class_id or 0, callback.from_user)
        if not context:
            await callback.answer("به این کلاس دسترسی نداری.", show_alert=True)
            return
        permitted[form_name] = (
            context["is_school_admin"]
            or context["is_platform_admin"]
            or context["is_manager"]
            or (form_name == "subject_add" and context["is_approver"])
        )
        prompts["class_code"] = "برای این کلاس کدهای اتصال گروه و عضویت دانش‌آموز ساخته می‌شوند."
    if not permitted.get(form_name, False):
        await callback.answer("برای این عملیات مجوز نداری.", show_alert=True)
        return
    if form_name == "class_code":
        await _create_class_codes(callback, state, class_id or 0)
        return
    await state.set_state(PanelInput.input)
    await state.update_data(
        panel_form=form_name,
        panel_class_id=class_id,
        panel_school_id=school_id,
        **({"school_name": None} if form_name == "school_new" else {}),
    )
    await _render_from_callback(
        callback,
        state,
        f"📝 <b>اطلاعات موردنیاز</b>\n{prompts[form_name]}",
        _markup([], "ui:cancel_input"),
    )


def _parent_for_form(
    form_name: str,
    class_id: int | None,
    school_id: int | None = None,
) -> str:
    if form_name == "join":
        return "ui:classes"
    if form_name == "search" and class_id is not None:
        return f"ui:class:{class_id}"
    if class_id is not None:
        return f"ui:manage:{class_id}"
    if form_name in {"year_new", "class_new"} and school_id is not None:
        return f"ui:school_manage:{school_id}"
    if form_name in {"school_new", "school_admin_add"}:
        return "ui:platform"
    return "ui:school"


async def _create_class_codes(callback: CallbackQuery, state: FSMContext, class_id: int) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            context = await get_class_with_school(session, class_id)
            if not context or not await user_can_access_class(session, user, class_id):
                await callback.answer("کلاس پیدا نشد یا دسترسی نداری.", show_alert=True)
                return
            class_group, _, school = context
            manager = await session.scalar(
                select(ClassMembership.id).where(
                    ClassMembership.class_id == class_id,
                    ClassMembership.user_id == user.id,
                    ClassMembership.role == "class_manager",
                    ClassMembership.is_active.is_(True),
                )
            )
            if not user.is_platform_admin and not await is_school_admin(session, user, school.id) and manager is None:
                await callback.answer("مجوز ساخت کد نداری.", show_alert=True)
                return
            connect_code = make_code()
            join_code = make_code()
            from datetime import timedelta, timezone
            now = datetime.now(timezone.utc)
            class_group.group_link_code_hash = hash_code(connect_code)
            class_group.group_link_code_expires_at = now + timedelta(minutes=30)
            class_group.member_join_code_hash = hash_code(join_code)
            class_group.member_join_code_expires_at = now + timedelta(days=7)
            class_name = class_group.name
    await _render_from_callback(
        callback,
        state,
        f"🔐 <b>کدهای کلاس {escape(class_name)}</b>\n"
        f"کد اتصال گروه (یک‌بارمصرف، ۳۰ دقیقه):\n<code>{connect_code}</code>\n\n"
        f"کد عضویت دانش‌آموزان (۷ روز):\n<code>{join_code}</code>\n\n"
        "کد اتصال را در گروه مقصد، توسط مدیر گروه، وارد کنید.",
        _markup([], f"ui:manage:{class_id}"),
    )


@router.message(
    F.chat.type == "private",
    F.text.startswith("/") & ~F.text.startswith("/start"),
)
async def private_slash_command_notice(message: Message):
    _, roles = await _current_user(message)
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    await message.answer(
        "در گفت‌وگوی خصوصی، فرمان تایپ نکن. عملیات را از دکمه‌های پایین صفحه انتخاب کن.",
        reply_markup=main_reply_keyboard(
            is_platform_admin=roles["platform"],
            is_school_admin=roles["school"],
            is_class_manager=roles["manager"],
            is_approver=roles["approver"],
        ),
    )


@router.message(F.chat.type == "private", F.text.in_(MAIN_BUTTONS))
async def main_button_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    label = (message.text or "").strip()
    saved = await state.get_data()
    panel_message_id = saved.get("panel_message_id")
    await state.clear()
    await state.update_data(
        **({"panel_message_id": panel_message_id} if panel_message_id is not None else {}),
        reply_keyboard_visible=True,
    )
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    if label == "⬅️ بازگشت":
        if panel_message_id is not None:
            try:
                await bot.delete_message(message.chat.id, panel_message_id)
            except TelegramBadRequest:
                pass
        await state.clear()
        await state.update_data(reply_keyboard_visible=True)
        _, roles = await _current_user(message)
        await message.answer(
            "به منوی اصلی برگشتی.",
            reply_markup=main_reply_keyboard(
                is_platform_admin=roles["platform"],
                is_school_admin=roles["school"],
                is_class_manager=roles["manager"],
                is_approver=roles["approver"],
            ),
        )
        return
    if label == "ℹ️ راهنمای استفاده":
        await _render_from_message(
            message, state, bot,
            "ℹ️ <b>راهنمای ClassMate</b>\n"
            "تمام عملیات از دکمه‌های کیبورد پایین صفحه آغاز می‌شوند.\n"
            "برای ورود به کلاس از «عضویت در کلاس» استفاده کن؛ برای ثبت تکلیف، امتحان یا گزارش «ثبت اطلاعات» را بزن.\n"
            "هر پنل داخلی دکمه بازگشت دارد و فقط نقش‌های مجاز، گزینه‌های مدیریتی و صف بررسی را می‌بینند.",
            _markup([]),
        )
        return
    if label == "🔑 عضویت در کلاس":
        await state.set_state(PanelInput.input)
        await state.update_data(panel_form="join", panel_class_id=None)
        await _render_from_message(
            message, state, bot,
            "🔑 کد دعوت کلاس را وارد کن. این کد را از مدیر کلاس دریافت کن.",
            _markup([], "ui:cancel_input"),
        )
        return
    if label == "🏫 کلاس‌های من":
        classes = await _get_classes(message.from_user)
        if not classes:
            await _render_from_message(
                message, state, bot,
                "هنوز عضو هیچ کلاسی نیستی. دکمه «🔑 عضویت در کلاس» را بزن و کد دعوت را وارد کن.",
                _markup([]),
            )
        else:
            await _render_from_message(
                message, state, bot, "کلاس‌های قابل‌دسترسی:",
                _markup([
                    [_button(f"🏫 {name[:48]}", f"ui:class:{class_id}")]
                    for class_id, name in classes
                ]),
            )
        return
    if label == "➕ ثبت اطلاعات":
        rows = [
            [_button("📚 ثبت تکلیف", "new:homework"), _button("📝 ثبت امتحان", "new:exam")],
            [_button("🗒 ثبت گزارش روزانه", "new:report")],
        ]
        await _render_from_message(message, state, bot, "چه اطلاعاتی می‌خواهی ثبت کنی؟", _markup(rows))
        return
    actions = {
        "📚 تکالیف": "homework",
        "📝 امتحانات": "exams",
        "📅 برنامه هفتگی": "week",
        "🗒 گزارش امروز": "today",
        "🔎 جست‌وجو": "ask",
        "🔔 یادآوری": "reminders",
        "🕓 صف بررسی": "pending",
        "⚙️ مدیریت کلاس": "manage",
    }
    if label in actions:
        await _show_class_selector_from_message(
            message, state, bot, message.from_user, actions[label]
        )
        return
    if label == "🏢 مدیریت مدرسه":
        _, roles = await _current_user(message)
        if not roles["school"]:
            await _render_from_message(message, state, bot, "این بخش فقط برای مدیر مدرسه است.", _markup([]))
            return
        schools = await _admin_schools(message.from_user)
        rows = [
            [_button(f"🏫 {name[:42]}", f"ui:school_manage:{school_id}")]
            for school_id, name in schools
        ]
        await _render_from_message(
            message, state, bot,
            "🏢 <b>مدیریت مدرسه</b>\nمدرسه‌ای را برای مدیریت انتخاب کن.",
            _markup(rows),
        )
        return
    if label == "🛡 پنل مدیر کل":
        _, roles = await _current_user(message)
        if not roles["platform"]:
            await _render_from_message(message, state, bot, "این پنل فقط برای مدیر کل است.", _markup([]))
            return
        rows = [
            [_button("🏫 ایجاد مدرسه و سال تحصیلی", "ui:form:school_new")],
            [_button("👤 تعیین مدیر مدرسه", "ui:school_admin_schools")],
        ]
        await _render_from_message(message, state, bot, "🛡 <b>پنل مدیر کل ClassMate</b>", _markup(rows))
        return


@router.callback_query(F.data == "ui:close")
async def close_panel(callback: CallbackQuery, state: FSMContext):
    await _close_panel(callback, state)


@router.callback_query(F.data == "ui:classes")
async def classes_panel(callback: CallbackQuery, state: FSMContext):
    await _show_classes(callback, state, callback.from_user)


@router.callback_query(F.data.startswith("ui:class:"))
async def class_panel(callback: CallbackQuery, state: FSMContext):
    class_id = int(callback.data.rsplit(":", 1)[1])
    await _show_class_panel(callback, state, class_id)


@router.callback_query(F.data.startswith("ui:action:"))
async def selected_action(callback: CallbackQuery, state: FSMContext, bot: Bot):
    _, _, action, class_id_raw = callback.data.split(":")
    class_id = int(class_id_raw)
    await callback.answer()
    await _show_action(callback.message, state, bot, callback.from_user, action, class_id)


@router.callback_query(F.data.startswith("ui:homework:") | F.data.startswith("ui:exams:") | F.data.startswith("ui:today:") | F.data.startswith("ui:week:") | F.data.startswith("ui:reminders:") | F.data.startswith("ui:ask:") | F.data.startswith("ui:pending:") | F.data.startswith("ui:manage:"))
async def class_action(callback: CallbackQuery, state: FSMContext, bot: Bot):
    action, class_id_raw = callback.data.split(":")[1:]
    class_id = int(class_id_raw)
    context = await _class_context(class_id, callback.from_user)
    if not context:
        await callback.answer("به این کلاس دسترسی نداری.", show_alert=True)
        return
    if action == "pending":
        await callback.answer()
        await _show_pending_from_message(callback.message, state, bot, class_id, callback.from_user)
        return
    if action == "manage":
        await callback.answer()
        await _show_management_from_message(callback.message, state, bot, class_id, callback.from_user)
        return
    if action == "ask":
        await state.set_state(PanelInput.input)
        await state.update_data(panel_form="search", panel_class_id=class_id)
        await _render_from_callback(
            callback,
            state,
            "🔎 پرسشت را درباره تکلیف یا امتحان بنویس. فقط اطلاعات تأییدشده جست‌وجو می‌شود.",
            _markup([], f"ui:class:{class_id}"),
        )
        return
    # Render views through a lightweight message adapter so there is one panel message.
    await callback.answer()
    await _show_action(callback.message, state, bot, callback.from_user, action, class_id)


@router.callback_query(F.data.startswith("ui:reminder_toggle:"))
async def reminder_toggle(callback: CallbackQuery, state: FSMContext, bot: Bot):
    _, _, class_id_raw, category, setting = callback.data.split(":")
    class_id = int(class_id_raw)
    context = await _class_context(class_id, callback.from_user)
    if not context:
        await callback.answer("به این کلاس دسترسی نداری.", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            preference = await session.scalar(
                select(NotificationPreference).where(
                    NotificationPreference.user_id == user.id,
                    NotificationPreference.class_id == class_id,
                )
            )
            if preference is None:
                preference = NotificationPreference(user_id=user.id, class_id=class_id)
                session.add(preference)
            enabled = setting == "on"
            if category == "homework":
                preference.homework_enabled = enabled
            elif category == "exams":
                preference.exams_enabled = enabled
    await callback.answer("تنظیم یادآوری ذخیره شد.")
    # Re-render using a private Message action and the same panel message ID.
    await _show_action(callback.message, state, bot, callback.from_user, "reminders", class_id)


@router.callback_query(F.data == "ui:platform")
async def platform_panel(callback: CallbackQuery, state: FSMContext):
    await _show_platform_panel(callback, state)


@router.callback_query(F.data == "ui:school")
async def school_panel(callback: CallbackQuery, state: FSMContext):
    await _show_school_panel(callback, state)


@router.callback_query(F.data.startswith("ui:school_manage:"))
async def school_management_panel(callback: CallbackQuery, state: FSMContext):
    school_id = int(callback.data.rsplit(":", 1)[1])
    await _show_school_management(callback, state, school_id)


@router.callback_query(F.data == "ui:school_admin_schools")
async def school_admin_school_selector(callback: CallbackQuery, state: FSMContext):
    _, roles = await _current_user(callback)
    if not roles["platform"]:
        await callback.answer("این عملیات فقط برای مدیر کل است.", show_alert=True)
        return
    schools = await _admin_schools(callback.from_user)
    rows = [
        [_button(f"🏫 {name[:42]}", f"ui:form:school_admin_add:{school_id}")]
        for school_id, name in schools
    ]
    await _render_from_callback(
        callback, state,
        "مدرسه‌ای را برای تعیین مدیر انتخاب کن:",
        _markup(rows, "ui:platform"),
    )


@router.callback_query(F.data.startswith("ui:school_classes:"))
async def school_class_manager_selector(callback: CallbackQuery, state: FSMContext):
    school_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            school = await session.get(School, school_id)
            if not school or not await is_school_admin(session, user, school_id):
                await callback.answer("به این مدرسه دسترسی نداری.", show_alert=True)
                return
            classes = list((await session.scalars(
                select(ClassGroup)
                .join(AcademicYear, ClassGroup.academic_year_id == AcademicYear.id)
                .where(
                    AcademicYear.school_id == school_id,
                    ClassGroup.is_active.is_(True),
                )
                .order_by(ClassGroup.name)
            )).all())
    rows = [
        [_button(f"👤 {item.name[:42]}", f"ui:form:class_manager_add:{item.id}")]
        for item in classes
    ]
    await _render_from_callback(
        callback, state,
        "کلاسی را برای تعیین مدیر انتخاب کن:" if classes else "هنوز کلاسی در این مدرسه ثبت نشده است.",
        _markup(rows, f"ui:school_manage:{school_id}"),
    )


@router.callback_query(F.data.startswith("ui:school_classes_view:"))
async def school_classes_view(callback: CallbackQuery, state: FSMContext):
    school_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            school = await session.get(School, school_id)
            if not school or not await is_school_admin(session, user, school_id):
                await callback.answer("به این مدرسه دسترسی نداری.", show_alert=True)
                return
            rows_data = list((await session.execute(
                select(ClassGroup, AcademicYear)
                .join(AcademicYear, ClassGroup.academic_year_id == AcademicYear.id)
                .where(
                    AcademicYear.school_id == school_id,
                    ClassGroup.is_active.is_(True),
                )
                .order_by(AcademicYear.name.desc(), ClassGroup.name)
            )).all())
    rows = [
        [_button(f"🏫 {item.name[:36]} · {item.grade[:12]}", f"ui:class:{item.id}")]
        for item, _year in rows_data
    ]
    body = "📚 کلاس‌های مدرسه:" if rows else "برای این مدرسه هنوز کلاسی ثبت نشده است."
    await _render_from_callback(
        callback, state, body, _markup(rows, f"ui:school_manage:{school_id}")
    )


@router.callback_query(F.data.startswith("ui:form:"))
async def form_panel(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    form_name = parts[2]
    context_id = int(parts[3]) if len(parts) > 3 else None
    if form_name in {"year_new", "class_new", "school_admin_add"}:
        await _show_form(callback, state, form_name, school_id=context_id)
    else:
        await _show_form(callback, state, form_name, class_id=context_id)


@router.callback_query(F.data == "ui:cancel_input")
async def cancel_input(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    form_name = data.get("panel_form")
    class_id = data.get("panel_class_id")
    school_id = data.get("panel_school_id")
    await state.set_state(None)
    await state.update_data(
        panel_form=None,
        panel_school_id=None,
        **({"school_name": None} if form_name == "school_new" else {}),
    )
    parent = _parent_for_form(form_name or "", class_id, school_id)
    if parent == "ui:platform":
        await _show_platform_panel(callback, state)
    elif parent == "ui:school":
        await _show_school_panel(callback, state)
    elif parent.startswith("ui:school_manage:"):
        await _show_school_management(callback, state, int(parent.rsplit(":", 1)[1]))
    elif parent == "ui:classes":
        await _show_classes(callback, state, callback.from_user)
    elif parent.startswith("ui:class:"):
        await _show_class_panel(callback, state, int(parent.rsplit(":", 1)[1]))
    elif class_id:
        await _show_management_from_message(callback.message, state, callback.bot, class_id, callback.from_user)
    else:
        await _close_panel(callback, state)


@router.callback_query(F.data.startswith("ui:review:"))
async def review_callback(callback: CallbackQuery, state: FSMContext):
    try:
        parts = callback.data.split(":")
        if len(parts) != 6:
            raise ValueError
        _, _, action, entity_type, entity_id_raw, class_id_raw = parts
        entity_id, class_id = int(entity_id_raw), int(class_id_raw)
        if (
            action not in {"approve", "reject"}
            or entity_type not in {"homework", "exam", "report"}
            or entity_id < 1
            or class_id < 1
        ):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        await callback.answer("درخواست بررسی نامعتبر یا منقضی شده است.", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            context = await get_class_with_school(session, class_id)
            if not context or not await can_approve(session, user, class_id):
                await callback.answer("مجوز بررسی این کلاس را نداری.", show_alert=True)
                return
            class_group, _, school = context
            submitter_telegram_id = await get_record_submitter_telegram_id(
                session, entity_type, entity_id, class_id
            )
            if action == "approve":
                success, response = await approve_record(
                    session, entity_type, entity_id, user.id, school.id,
                    class_id, school.timezone,
                )
            else:
                success, response = await reject_record(
                    session, entity_type, entity_id, user.id, school.id,
                    class_id, "رد شده توسط فرد مورد اعتماد",
                )
    await callback.answer(response, show_alert=not success)
    if success:
        entity_labels = {
            "homework": "تکلیف",
            "exam": "امتحان",
            "report": "گزارش روزانه",
        }
        entity_label = entity_labels.get(entity_type, "اطلاعات")
        if class_group.group_chat_id:
            try:
                await callback.bot.send_message(
                    class_group.group_chat_id,
                    f"{'✅' if action == 'approve' else '❌'} "
                    f"{entity_label} ثبت‌شده بررسی شد.",
                )
            except Exception:
                logger.exception("Failed to notify class group after review")
        if submitter_telegram_id is not None:
            try:
                message_text = (
                    f"✅ {entity_label} ثبت‌شدهٔ تو تأیید شد و اکنون در اطلاعات قطعی کلاس نمایش داده می‌شود."
                    if action == "approve"
                    else f"❌ {entity_label} ثبت‌شدهٔ تو رد شد. برای جزئیات با مدیر کلاس تماس بگیر."
                )
                await callback.bot.send_message(submitter_telegram_id, message_text)
            except Exception:
                logger.info(
                    "Could not privately notify submitter %s about %s #%s",
                    submitter_telegram_id, entity_type, entity_id,
                    exc_info=True,
                )
        await _show_pending_from_message(
            callback.message, state, callback.bot, class_id, callback.from_user
        )

def _parse_persian_int(value: str) -> int:
    """Parse integers written with Persian or Arabic-Indic digits as well as ASCII."""
    digit_map = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )
    normalized = value.strip().translate(digit_map)
    for separator in (",", "٬", " ", "\u00a0", "\u202f", "'"):
        normalized = normalized.replace(separator, "")
    if not normalized:
        raise ValueError("عدد واردشده خالی است.")
    return int(normalized)


async def _handle_panel_input(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    data = await state.get_data()
    form_name = data.get("panel_form")
    class_id = data.get("panel_class_id")
    school_id = data.get("panel_school_id")
    await state.set_state(None)
    if not form_name:
        await _render_from_message(message, state, bot, "این فرم دیگر فعال نیست.", _markup([]))
        return

    if form_name == "school_new" and not data.get("school_name"):
        if not raw or len(raw) > 180:
            await state.set_state(PanelInput.input)
            await _render_from_message(
                message, state, bot,
                "نام مدرسه باید بین ۱ تا ۱۸۰ نویسه باشد. دوباره وارد کن.",
                _markup([], "ui:cancel_input"),
            )
            return
        await state.update_data(school_name=raw)
        await state.set_state(PanelInput.input)
        await _render_from_message(
            message, state, bot,
            "سال تحصیلی را وارد کن.\nمثال: ۱۴۰۵-۱۴۰۶",
            _markup([], "ui:cancel_input"),
        )
        return

    school_group_token = None
    school_group_url = None
    try:
        if form_name == "school_new":
            school_name = data.get("school_name", "").strip()
            if not school_name or not raw or len(raw) > 40:
                raise ValueError("سال تحصیلی باید بین ۱ تا ۴۰ نویسه باشد.")
            bot_profile = await bot.get_me()
            if not bot_profile.username:
                raise ValueError("شناسه عمومی ربات در تلگرام در دسترس نیست؛ ساخت مدرسه انجام نشد.")
        async with SessionFactory() as session:
            async with session.begin():
                actor = await _user(session, message.from_user, private=True)
                result_text = ""
                if form_name == "join":
                    code = raw.strip()
                    from datetime import timezone
                    class_group = await session.scalar(
                        select(ClassGroup).where(
                            ClassGroup.member_join_code_hash == hash_code(code),
                            ClassGroup.member_join_code_expires_at > datetime.now(timezone.utc),
                            ClassGroup.is_active.is_(True),
                        )
                    )
                    if class_group is None:
                        raise ValueError("کد دعوت معتبر نیست یا اعتبار آن تمام شده است.")
                    await _ensure_group_member(session, actor, class_group.id)
                    result_text = f"✅ به کلاس «{escape(class_group.name)}» متصل شدی."
                elif form_name == "school_new":
                    if not actor.is_platform_admin:
                        raise PermissionError("فقط مدیر کل می‌تواند مدرسه ایجاد کند.")
                    timezone_name = get_settings().default_timezone
                    school = School(name=school_name[:180], timezone=timezone_name)
                    school_group_token = f"CM_{make_code()}"
                    school.group_link_token_hash = hash_code(school_group_token)
                    school.group_link_token_expires_at = (
                        datetime.now(timezone.utc) + timedelta(hours=24)
                    )
                    session.add(school)
                    await session.flush()
                    year = AcademicYear(school_id=school.id, name=raw[:40], is_current=True)
                    session.add(year)
                    session.add(
                        SchoolMembership(
                            school_id=school.id,
                            user_id=actor.id,
                            role="school_admin",
                        )
                    )
                    await session.flush()
                    school_group_url = (
                        f"https://t.me/{bot_profile.username}?startgroup={school_group_token}"
                    )
                    result_text = (
                        f"✅ مدرسه «{escape(school.name)}» ایجاد شد.\n"
                        f"سال تحصیلی: {escape(year.name)}\n\n"
                        "مرحله بعد: دکمه زیر را بزن، گروه مدرسه را انتخاب کن و ClassMate را به آن اضافه کن. "
                        "این لینک تا ۲۴ ساعت معتبر است."
                    )
                elif form_name == "school_admin_add":
                    if not actor.is_platform_admin:
                        raise PermissionError("فقط مدیر کل می‌تواند مدیر مدرسه تعیین کند.")
                    if school_id is not None:
                        target_school_id = school_id
                        telegram_id = _parse_persian_int(raw)
                    else:
                        school_raw, telegram_raw = _parse_pipe_args(raw, 2)
                        target_school_id = _parse_persian_int(school_raw)
                        telegram_id = _parse_persian_int(telegram_raw)
                    school = await session.get(School, target_school_id)
                    target = await session.scalar(select(User).where(User.telegram_id == telegram_id))
                    if not school or not school.is_active or not target:
                        raise ValueError("مدرسه فعال یا کاربر پیدا نشد. کاربر باید ابتدا ربات را Start کرده باشد.")
                    membership = await session.scalar(
                        select(SchoolMembership).where(
                            SchoolMembership.school_id == target_school_id,
                            SchoolMembership.user_id == target.id,
                        )
                    )
                    if membership is None:
                        session.add(SchoolMembership(school_id=target_school_id, user_id=target.id, role="school_admin"))
                    else:
                        membership.role = "school_admin"
                        membership.is_active = True
                    result_text = "✅ مدیر مدرسه تعیین شد."
                elif form_name == "year_new":
                    if school_id is None:
                        raise ValueError("مدرسه انتخاب نشده است؛ از پنل مدیریت دوباره شروع کن.")
                    year_name = raw.strip()
                    if not year_name or len(year_name) > 40:
                        raise ValueError("نام سال تحصیلی باید بین ۱ تا ۴۰ نویسه باشد.")
                    if not await is_school_admin(session, actor, school_id):
                        raise PermissionError("مجوز مدیریت این مدرسه را نداری.")
                    school = await session.get(School, school_id)
                    if not school or not school.is_active:
                        raise ValueError("مدرسه فعال پیدا نشد.")
                    existing = await session.scalar(
                        select(AcademicYear.id).where(
                            AcademicYear.school_id == school_id,
                            AcademicYear.name == year_name,
                        )
                    )
                    if existing:
                        raise ValueError("این سال تحصیلی قبلاً ثبت شده است.")
                    current = (
                        await session.execute(
                            select(AcademicYear).where(
                                AcademicYear.school_id == school_id,
                                AcademicYear.is_current.is_(True),
                            )
                        )
                    ).scalars().all()
                    for item in current:
                        item.is_current = False
                    year = AcademicYear(school_id=school_id, name=year_name[:40], is_current=True)
                    session.add(year)
                    await session.flush()
                    result_text = f"✅ سال تحصیلی جدید فعال شد. شناسه: <code>{year.id}</code>"
                elif form_name == "class_new":
                    if school_id is None:
                        raise ValueError("مدرسه انتخاب نشده است؛ از پنل مدیریت دوباره شروع کن.")
                    grade, section, name = _parse_pipe_args(raw, 3)
                    if not grade or not name:
                        raise ValueError("پایه و نام کلاس الزامی هستند.")
                    if len(grade) > 32 or len(section) > 32 or len(name) > 80:
                        raise ValueError("حداکثر طول پایه و شعبه ۳۲ و نام کلاس ۸۰ نویسه است.")
                    school = await session.get(School, school_id)
                    if (
                        not school or not school.is_active
                        or not await is_school_admin(session, actor, school_id)
                    ):
                        raise PermissionError("مدرسه فعال پیدا نشد یا مجوز مدیریت آن را نداری.")
                    year = await session.scalar(
                        select(AcademicYear).where(
                            AcademicYear.school_id == school_id,
                            AcademicYear.is_current.is_(True),
                        )
                    )
                    if not year:
                        raise ValueError("برای این مدرسه سال تحصیلی جاری تعریف نشده است.")
                    item = ClassGroup(
                        academic_year_id=year.id,
                        name=name[:80],
                        grade=grade[:32],
                        section=None if section in {"", "-"} else section,
                    )
                    session.add(item)
                    await session.flush()
                    session.add(ClassMembership(class_id=item.id, user_id=actor.id, role="class_manager"))
                    result_text = f"✅ کلاس ایجاد شد. شناسه کلاس: <code>{item.id}</code>"
                elif form_name == "class_manager_add":
                    if class_id is not None:
                        target_class_id = class_id
                        telegram_id = _parse_persian_int(raw)
                    else:
                        class_raw, telegram_raw = _parse_pipe_args(raw, 2)
                        target_class_id = _parse_persian_int(class_raw)
                        telegram_id = _parse_persian_int(telegram_raw)
                    context = await get_class_with_school(session, target_class_id)
                    target = await session.scalar(select(User).where(User.telegram_id == telegram_id))
                    if not context or not target:
                        raise ValueError("کلاس یا کاربر پیدا نشد؛ کاربر باید ربات را Start کرده باشد.")
                    if not await is_school_admin(session, actor, context[2].id):
                        raise PermissionError("فقط مدیر مدرسه می‌تواند مدیر کلاس تعیین کند.")
                    membership = await session.scalar(
                        select(ClassMembership).where(
                            ClassMembership.class_id == target_class_id,
                            ClassMembership.user_id == target.id,
                        )
                    )
                    if membership is None:
                        session.add(ClassMembership(class_id=target_class_id, user_id=target.id, role="class_manager"))
                    else:
                        membership.role = "class_manager"
                        membership.is_active = True
                    result_text = "✅ مدیر کلاس تعیین شد."
                elif form_name == "approver_add":
                    if class_id is None:
                        raise ValueError("شناسه کلاس مشخص نیست.")
                    context = await get_class_with_school(session, class_id)
                    if not context:
                        raise ValueError("کلاس پیدا نشد.")
                    class_group, _, school = context
                    manager = await session.scalar(
                        select(ClassMembership.id).where(
                            ClassMembership.class_id == class_id,
                            ClassMembership.user_id == actor.id,
                            ClassMembership.role == "class_manager",
                            ClassMembership.is_active.is_(True),
                        )
                    )
                    if not actor.is_platform_admin and not await is_school_admin(session, actor, school.id) and manager is None:
                        raise PermissionError("مجوز مدیریت این کلاس را نداری.")
                    telegram_id = int(raw)
                    target = await session.scalar(select(User).where(User.telegram_id == telegram_id))
                    if not target:
                        raise ValueError("این کاربر باید ابتدا ربات را در خصوصی Start کند.")
                    membership = await session.scalar(
                        select(ClassMembership).where(
                            ClassMembership.class_id == class_id,
                            ClassMembership.user_id == target.id,
                        )
                    )
                    if membership is None:
                        session.add(ClassMembership(class_id=class_id, user_id=target.id, role="trusted_approver"))
                    else:
                        membership.role = "trusted_approver"
                        membership.is_active = True
                    result_text = "✅ فرد مورد اعتماد به این کلاس اضافه شد."
                elif form_name == "subject_add":
                    if class_id is None:
                        raise ValueError("شناسه کلاس مشخص نیست.")
                    context = await get_class_with_school(session, class_id)
                    if not context:
                        raise ValueError("کلاس پیدا نشد.")
                    class_group, _, school = context
                    manager = await session.scalar(
                        select(ClassMembership.id).where(
                            ClassMembership.class_id == class_id,
                            ClassMembership.user_id == actor.id,
                            ClassMembership.role.in_(["class_manager", "trusted_approver"]),
                            ClassMembership.is_active.is_(True),
                        )
                    )
                    if not actor.is_platform_admin and not await is_school_admin(session, actor, school.id) and manager is None:
                        raise PermissionError("برای افزودن درس مجوز نداری.")
                    subject = await add_subject(session, class_id, school.id, raw.strip())
                    result_text = f"✅ درس «{escape(subject.name)}» به کلاس افزوده شد."
                elif form_name == "timetable_set":
                    if class_id is None:
                        raise ValueError("شناسه کلاس مشخص نیست.")
                    day_text, period_text, subject_name, time_range = _parse_pipe_args(raw, 4)
                    day_text = day_text.strip()
                    day_map = {day: index for index, day in enumerate(WEEKDAYS)}
                    weekday = day_map.get(day_text)
                    if weekday is None:
                        weekday = _parse_persian_int(day_text)
                    period = _parse_persian_int(period_text)
                    if weekday not in range(7) or period < 1:
                        raise ValueError("روز یا شماره زنگ معتبر نیست.")
                    if time_range == "-":
                        starts_at = ends_at = None
                    else:
                        start_raw, end_raw = time_range.split("-", 1)
                        sh, sm = (int(part) for part in start_raw.split(":"))
                        eh, em = (int(part) for part in end_raw.split(":"))
                        starts_at, ends_at = time(sh, sm), time(eh, em)
                        if starts_at >= ends_at:
                            raise ValueError("ساعت پایان باید بعد از ساعت شروع باشد.")
                    context = await get_class_with_school(session, class_id)
                    if not context:
                        raise ValueError("کلاس پیدا نشد.")
                    class_group, _, school = context
                    manager = await session.scalar(
                        select(ClassMembership.id).where(
                            ClassMembership.class_id == class_id,
                            ClassMembership.user_id == actor.id,
                            ClassMembership.role == "class_manager",
                            ClassMembership.is_active.is_(True),
                        )
                    )
                    if not actor.is_platform_admin and not await is_school_admin(session, actor, school.id) and manager is None:
                        raise PermissionError("فقط مدیر مدرسه یا مدیر کلاس می‌تواند برنامه را تغییر دهد.")
                    subject = await get_subject(session, class_id, school.id, subject_name)
                    if subject is None:
                        raise ValueError("این درس به کلاس متصل نیست؛ ابتدا آن را اضافه کن.")
                    entry = await session.scalar(
                        select(TimetableEntry).where(
                            TimetableEntry.class_id == class_id,
                            TimetableEntry.weekday == weekday,
                            TimetableEntry.period == period,
                        )
                    )
                    if entry is None:
                        entry = TimetableEntry(
                            class_id=class_id, subject_id=subject.id, weekday=weekday,
                            period=period, starts_at=starts_at, ends_at=ends_at,
                        )
                        session.add(entry)
                        await session.flush()
                    else:
                        entry.subject_id = subject.id
                        entry.starts_at = starts_at
                        entry.ends_at = ends_at
                    await audit(
                        session, school.id, class_id, actor.id, "timetable_set",
                        "timetable_entry", entry.id,
                        {"weekday": weekday, "period": period, "subject": subject.name},
                    )
                    result_text = (
                        f"✅ برنامه ثبت شد: {WEEKDAYS[weekday]}، زنگ {period}، "
                        f"{escape(subject.name)}"
                    )
                else:
                    raise ValueError("این فرم شناخته نشده است.")
        await state.update_data(
            panel_form=None,
            panel_school_id=None,
            school_name=None,
        )
        if form_name == "join":
            back = "ui:classes"
        elif class_id:
            back = f"ui:manage:{class_id}"
        elif form_name in {"year_new", "class_new"} and school_id is not None:
            back = f"ui:school_manage:{school_id}"
        elif form_name in {"school_new", "school_admin_add"}:
            back = "ui:platform"
        else:
            back = "ui:school"
        result_rows = []
        if school_group_url:
            result_rows.append([
                InlineKeyboardButton(
                    text="➕ افزودن ربات به گروه مدرسه",
                    url=school_group_url,
                )
            ])
        await _render_from_message(
            message, state, bot, result_text, _markup(result_rows, back)
        )
    except (ValueError, PermissionError, TypeError, KeyError) as exc:
        await state.set_state(PanelInput.input)
        await _render_from_message(
            message,
            state,
            bot,
            f"⚠️ {escape(str(exc))}\n\nاطلاعات را دوباره وارد کن یا با دکمه بازگشت خارج شو.",
            _markup([], "ui:cancel_input"),
        )


async def _handle_search_input(message: Message, state: FSMContext, bot: Bot) -> None:
    query_text = (message.text or "").strip()
    data = await state.get_data()
    class_id = data.get("panel_class_id")
    context = await _class_context(int(class_id or 0), message.from_user)
    if not context:
        await state.set_state(None)
        await _render_from_message(message, state, bot, "به این کلاس دسترسی نداری.", _markup([]))
        return
    class_group, school = context["class"], context["school"]
    normalized = normalize_subject_name(query_text)
    if any(word in normalized for word in ("امتحان", "آزمون", "کوئیز")):
        async with SessionFactory() as session:
            async with session.begin():
                rows = await list_exams(session, class_group.id)
        answer = _format_exams(rows, school.timezone)
    else:
        next_phrases = (
            "برای جلسه آینده", "برای جلسه بعدی", "برای جلسه بعد",
            "جلسه آینده", "جلسه بعدی", "جلسه بعد",
        )
        asks_next = any(phrase in normalized for phrase in next_phrases)
        subject_query = normalized
        for phrase in (*next_phrases, "چی داریم", "چه داریم", "چه تکلیفی داریم", "تکلیف", "درس"):
            subject_query = subject_query.replace(phrase, " ")
        for mark in ("؟", "?", "!", "،", ",", ":", ";", "."):
            subject_query = subject_query.replace(mark, " ")
        subject_query = " ".join(subject_query.split())
        async with SessionFactory() as session:
            async with session.begin():
                if asks_next:
                    subject = await get_subject(session, class_group.id, school.id, subject_query)
                    if subject is None:
                        answer = "نام درس را در فهرست این کلاس پیدا نکردم. نام دقیق درس را وارد کن."
                    else:
                        lesson = await get_or_create_next_lesson(
                            session, class_group.id, subject.id, school.timezone
                        )
                        if lesson is None:
                            answer = "برای این درس جلسه آینده‌ای در برنامه هفتگی پیدا نشد."
                        else:
                            rows = await list_homework_for_lesson(
                                session, class_group.id, subject.id, lesson
                            )
                            answer = (
                                f"📅 <b>جلسه بعد {escape(subject.name)}</b>\n"
                                f"زمان: {jalali_datetime(lesson.starts_at, school.timezone)}\n\n"
                                + (_format_homework(rows) if rows else "برای این جلسه تکلیف تأییدشده‌ای ثبت نشده است.")
                            )
                else:
                    rows = await list_homework(session, class_group.id, subject_query or None)
                    answer = _format_homework(rows)
    await state.set_state(None)
    await _render_from_message(
        message, state, bot,
        f"🔎 <b>نتیجه جست‌وجو</b>\n{answer}\n\n"
        "<i>نتیجه فقط از اطلاعات تأییدشده کلاس استخراج شده است.</i>",
        _markup([], f"ui:class:{class_group.id}"),
    )


@router.message(PanelInput.input)
async def panel_text_input(message: Message, state: FSMContext, bot: Bot):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    data = await state.get_data()
    if data.get("panel_form") == "search":
        await _handle_search_input(message, state, bot)
    else:
        await _handle_panel_input(message, state, bot)
