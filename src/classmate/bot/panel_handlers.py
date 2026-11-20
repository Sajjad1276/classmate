from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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

from classmate.bot.context import (
    WEEKDAYS,
    _classes_for_user,
    _user,
)
from classmate.bot.formatters import _format_exams, _format_homework
from classmate.bot.panel_form_processing import handle_panel_input, handle_search_input
from classmate.bot.keyboards import main_reply_keyboard
from classmate.bot.states import PanelInput
from classmate.domain.enums import ENTITY_LABELS, EntityType, Role
from classmate.db.models import (
    AcademicYear,
    ClassGroup,
    ClassMembership,
    NotificationPreference,
    School,
    SchoolMembership,
    Subject,
    TimetableEntry,
)
from classmate.db.session import SessionFactory
from classmate.domain.dates import jalali_date
from classmate.services.permissions import (
    can_approve,
    get_class_with_school,
    is_school_admin,
    make_code,
    hash_code,
    user_can_access_class,
)
from classmate.services.notifications import update_user_reminder_preference
from classmate.services.group_bindings import lock_group_binding_mutations
from classmate.services.records import (
    approve_record,
    list_exams,
    list_homework,
    list_pending_records,
    list_reports,
    reject_record,
    cancel_approved_record,
    list_cancelable_records,
)

logger = logging.getLogger(__name__)
router = Router(name="classmate_panels")

def _is_panel_input_text(text: str | None) -> bool:
    """Accept ordinary form text, but let Telegram slash commands reach command handlers."""
    return bool(text and text.strip() and not text.lstrip().startswith("/"))


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
    if panel_id:
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
    # Attach the inline keyboard in the original send call so a form is never
    # displayed without its cancel/back control, even if a follow-up edit fails.
    sent = await bot.send_message(
        message.chat.id,
        text,
        reply_markup=keyboard if keyboard is not None else ReplyKeyboardRemove(),
    )
    await state.update_data(panel_message_id=sent.message_id)


async def _render_from_callback(
    callback: CallbackQuery,
    state: FSMContext,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await state.update_data(panel_message_id=callback.message.message_id)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            sent = await callback.message.answer(text, reply_markup=keyboard)
            await state.update_data(panel_message_id=sent.message_id)
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
                        SchoolMembership.role == Role.SCHOOL_ADMIN,
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
            class_manager = Role.CLASS_MANAGER in roles
            approver = school_admin or class_manager or Role.TRUSTED_APPROVER in roles
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
            is_manager = bool(membership and membership.role == Role.CLASS_MANAGER)
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
                "role": membership.role if membership else Role.SCHOOL_ADMIN,
            }


async def _show_cancelable_records_from_callback(
    callback: CallbackQuery,
    state: FSMContext,
    class_id: int,
    *,
    notice: str | None = None,
) -> None:
    context = await _class_context(class_id, callback.from_user)
    if not context or not context["is_approver"]:
        await callback.answer("برای لغو اطلاعات تأییدشده مجوز بررسی این کلاس را نداری.", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            items = await list_cancelable_records(session, class_id)
    if not items:
        body = "✅ در این کلاس رکورد تأییدشده‌ای برای لغو وجود ندارد."
        rows = []
    else:
        lines = ["🚫 <b>رکوردهای تأییدشده</b>", "برای لغو، مورد موردنظر را انتخاب کن:"]
        rows = []
        for entity_type, record in items:
            label = ENTITY_LABELS[EntityType(entity_type)]
            detail = record.title or "گزارش روزانه"
            lines.append(f"\n• <b>{escape(label)} #{record.id}</b> — {escape(detail[:100])}")
            rows.append([
                _button(
                    f"🚫 لغو {label} #{record.id}",
                    f"ui:cancel_confirm:{entity_type}:{record.id}:{class_id}",
                )
            ])
        body = "\n".join(lines)
    if notice:
        body = f"ℹ️ {escape(notice)}\n\n{body}"
    await _render_from_callback(callback, state, body, _markup(rows, f"ui:class:{class_id}"))


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
        rows.append([_button("🚫 لغو اطلاعات تأییدشده", f"ui:cancel_list:{class_id}")])
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
                reports = await list_reports(session, class_id, today)
        if not reports:
            body = "برای امروز گزارش تأییدشده‌ای ثبت نشده است."
        else:
            body = "\n\n".join(
                f"🗒 <b>{escape(report.title or 'گزارش روزانه')}</b>\n"
                f"{jalali_date(report.report_date)}\n{escape(report.body)}"
                for report in reports
            )
    elif action == "week":
        async with SessionFactory() as session:
            async with session.begin():
                entries = list(
                    (
                        await session.execute(
                            select(TimetableEntry, Subject)
                            .join(Subject, TimetableEntry.subject_id == Subject.id)
                            .where(
                                TimetableEntry.class_id == class_id,
                                TimetableEntry.deleted_at.is_(None),
                                Subject.deleted_at.is_(None),
                            )
                            .order_by(TimetableEntry.weekday, TimetableEntry.period)
                        )
                    ).all()
                )
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
        [_button("👤 تعیین مدیر مدرسه", "ui:form:school_admin_add")],
        [_button("📚 مدیریت مدرسه‌ها و کلاس‌ها", "ui:classes")],
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
    """List only active schools the current account is authorized to administer."""
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
                    SchoolMembership.role == Role.SCHOOL_ADMIN,
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
            actor = await _user(session, callback.from_user, private=True)
            school = await session.get(School, school_id)
            if (
                not school
                or not school.is_active
                or not await is_school_admin(session, actor, school_id)
            ):
                await callback.answer("به مدیریت این مدرسه دسترسی نداری.", show_alert=True)
                return
            school_name = school.name
            group_id = school.telegram_group_id
            group_title = school.telegram_group_title
            link_expires_at = school.group_link_token_expires_at
    link_is_pending = (
        link_expires_at is not None
        and link_expires_at > datetime.now(timezone.utc)
    )
    if group_id is not None:
        group_status = f"🟢 گروه مرکزی متصل: {escape(group_title or str(group_id))}"
        group_action = _button("🔄 تغییر گروه مرکزی", f"ui:school_group_reset_confirm:{school_id}")
    elif link_is_pending:
        group_status = "🕓 لینک اتصال صادر شده و تا ۲۴ ساعت معتبر است."
        group_action = _button("📨 ارسال لینک تازه", f"ui:school_group_link:{school_id}")
    else:
        group_status = "🔴 گروه مرکزی هنوز متصل نشده است."
        group_action = _button("🔗 ساخت لینک اتصال گروه", f"ui:school_group_link:{school_id}")
    rows = [
        [group_action],
        [_button("📅 ایجاد سال تحصیلی", f"ui:form:year_new:{school_id}")],
        [_button("🏫 ایجاد کلاس", f"ui:form:class_new:{school_id}")],
        [_button("👤 تعیین مدیر کلاس", f"ui:school_classes:{school_id}")],
        [_button("📚 مشاهده کلاس‌های این مدرسه", f"ui:school_classes_view:{school_id}")],
    ]
    await _render_from_callback(
        callback,
        state,
        f"🏢 <b>{escape(school_name)}</b>\n{group_status}\nعملیات مدیریتی مدرسه را انتخاب کن.",
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
        "school_admin_add": "شناسه مدرسه | شناسه عددی تلگرام کاربر",
        "year_new": "نام سال تحصیلی جدید را وارد کن.\nمثال: ۱۴۰۶-۱۴۰۷",
        "class_new": "پایه | شعبه | نام کلاس\nبرای نداشتن شعبه، - وارد کن.\nنمونه: دهم | الف | دهم تجربی ۱",
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
        "year_new": False,
        "class_new": False,
        "class_manager_add": False,
    }
    if form_name in {"year_new", "class_new"}:
        async with SessionFactory() as session:
            async with session.begin():
                actor = await _user(session, callback.from_user, private=True)
                permitted[form_name] = bool(
                    school_id is not None
                    and await is_school_admin(session, actor, school_id)
                )
    if form_name == "class_manager_add":
        context = await _class_context(class_id or 0, callback.from_user)
        if not context:
            await callback.answer("کلاس انتخاب‌شده پیدا نشد یا دسترسی نداری.", show_alert=True)
            return
        permitted[form_name] = context["is_school_admin"] or context["is_platform_admin"]
    if form_name in {"class_code", "approver_add", "subject_add", "timetable_set"}:
        context = await _class_context(class_id or 0, callback.from_user)
        if not context:
            await callback.answer("به این کلاس دسترسی نداری.", show_alert=True)
            return
        permitted[form_name] = (
            context["is_school_admin"]
            or context["is_platform_admin"]
            or context["is_manager"]
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
    form_name: str, class_id: int | None, school_id: int | None = None
) -> str:
    if form_name == "join":
        return "ui:close"
    if form_name == "search" and class_id is not None:
        return f"ui:class:{class_id}"
    if class_id is not None:
        return f"ui:manage:{class_id}"
    if school_id is not None:
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
                    ClassMembership.role == Role.CLASS_MANAGER,
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
    F.text.startswith("/") & ~F.text.startswith("/start") & ~F.text.startswith("/cancel_record"),
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
    if panel_message_id is not None:
        await state.update_data(panel_message_id=panel_message_id)
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
            "ℹ️ <b>راهنمای ClassMate</b>\\n"
            "تمام عملیات از دکمه‌های کیبورد پایین صفحه آغاز می‌شوند.\\n"
            "برای ورود به کلاس از «عضویت در کلاس» استفاده کن؛ برای ثبت تکلیف، امتحان یا گزارش «ثبت اطلاعات» را بزن.\\n"
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
        rows = [
            [_button("📅 ایجاد سال تحصیلی", "ui:form:year_new")],
            [_button("🏫 ایجاد کلاس", "ui:form:class_new")],
            [_button("👤 تعیین مدیر کلاس", "ui:form:class_manager_add")],
        ]
        await _render_from_message(message, state, bot, "🏢 <b>پنل مدیریت مدرسه</b>", _markup(rows))
        return
    if label == "🛡 پنل مدیر کل":
        _, roles = await _current_user(message)
        if not roles["platform"]:
            await _render_from_message(message, state, bot, "این پنل فقط برای مدیر کل است.", _markup([]))
            return
        rows = [
            [_button("🏫 ایجاد مدرسه و سال تحصیلی", "ui:form:school_new")],
            [_button("👤 تعیین مدیر مدرسه", "ui:form:school_admin_add")],
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


@router.callback_query(F.data.startswith("ui:cancel_list:"))
async def cancelable_records_callback(callback: CallbackQuery, state: FSMContext):
    try:
        class_id = int(callback.data.rsplit(":", 1)[1])
        if class_id < 1:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    await _show_cancelable_records_from_callback(callback, state, class_id)


@router.callback_query(F.data.startswith("ui:cancel_confirm:"))
async def cancel_record_confirm_callback(callback: CallbackQuery, state: FSMContext):
    try:
        _, _, entity_type, entity_id_raw, class_id_raw = callback.data.split(":")
        entity_id, class_id = int(entity_id_raw), int(class_id_raw)
        if (
            entity_type not in {EntityType.HOMEWORK, EntityType.EXAM, EntityType.REPORT}
            or entity_id < 1 or class_id < 1
        ):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        await callback.answer("درخواست لغو نامعتبر یا منقضی شده است.", show_alert=True)
        return
    context = await _class_context(class_id, callback.from_user)
    if not context or not context["is_approver"]:
        await callback.answer("برای لغو این رکورد مجوز نداری.", show_alert=True)
        return
    label = ENTITY_LABELS[EntityType(entity_type)]
    await _render_from_callback(
        callback,
        state,
        f"⚠️ <b>تأیید لغو {escape(label)} #{entity_id}</b>\n"
        "رکورد حذف فیزیکی نمی‌شود، اما از فهرست اطلاعات فعال کنار می‌رود و یادآوری‌هایش متوقف می‌شوند.\n"
        "این عملیات فقط برای رکوردهای تأییدشده معتبر است.",
        _markup(
            [[
                _button("بله، لغو شود", f"ui:cancel_execute:{entity_type}:{entity_id}:{class_id}"),
                _button("بازگشت", f"ui:cancel_list:{class_id}"),
            ]],
            f"ui:class:{class_id}",
        ),
    )


@router.callback_query(F.data.startswith("ui:cancel_execute:"))
async def cancel_record_execute_callback(callback: CallbackQuery, state: FSMContext):
    try:
        _, _, entity_type, entity_id_raw, class_id_raw = callback.data.split(":")
        entity_id, class_id = int(entity_id_raw), int(class_id_raw)
        if (
            entity_type not in {EntityType.HOMEWORK, EntityType.EXAM, EntityType.REPORT}
            or entity_id < 1 or class_id < 1
        ):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        await callback.answer("درخواست لغو نامعتبر یا منقضی شده است.", show_alert=True)
        return

    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            class_context = await get_class_with_school(session, class_id)
            if not class_context or not await can_approve(session, user, class_id):
                await callback.answer("برای لغو این رکورد مجوز نداری.", show_alert=True)
                return
            class_group, _, school = class_context
            success, response = await cancel_approved_record(
                session, entity_type, entity_id, user.id, school.id, class_id
            )
            group_chat_id = class_group.group_chat_id
    if success and group_chat_id is not None:
        label = ENTITY_LABELS[EntityType(entity_type)]
        try:
            await callback.bot.send_message(
                group_chat_id,
                f"🚫 {label} #{entity_id} لغو شد؛ یادآوری‌های آیندهٔ آن متوقف شدند.",
            )
        except Exception:
            logger.exception("Failed to announce cancelled %s #%s", entity_type, entity_id)
    await _show_cancelable_records_from_callback(
        callback, state, class_id, notice=response
    )


@router.callback_query(F.data.startswith("ui:reminder_toggle:"))
async def reminder_toggle(callback: CallbackQuery, state: FSMContext, bot: Bot):
    try:
        _, _, class_id_raw, category, setting = callback.data.split(":")
        class_id = int(class_id_raw)
        if class_id < 1 or category not in {"homework", "exams"} or setting not in {"off", "on"}:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        await callback.answer("درخواست تنظیم یادآوری نامعتبر است.", show_alert=True)
        return
    context = await _class_context(class_id, callback.from_user)
    if not context:
        await callback.answer("به این کلاس دسترسی نداری.", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            class_context = await get_class_with_school(session, class_id)
            if not class_context:
                await callback.answer("کلاس فعال پیدا نشد.", show_alert=True)
                return
            class_group, _, school = class_context
            created, cancelled = await update_user_reminder_preference(
                session, user, class_group, school.timezone, category, setting == "on"
            )
    if setting == "on":
        response = f"یادآوری فعال شد؛ {created} یادآوری آینده در صف قرار گرفت یا بازیابی شد."
    else:
        response = f"یادآوری خاموش شد؛ {cancelled} یادآوری خصوصی لغو شد."
    await callback.answer(response)
    await _show_action(callback.message, state, bot, callback.from_user, "reminders", class_id)


@router.callback_query(F.data == "ui:platform")
async def platform_panel(callback: CallbackQuery, state: FSMContext):
    await _show_platform_panel(callback, state)


@router.callback_query(F.data == "ui:school")
async def school_panel(callback: CallbackQuery, state: FSMContext):
    await _show_school_panel(callback, state)


@router.callback_query(F.data.startswith("ui:school_manage:"))
async def school_management_panel(callback: CallbackQuery, state: FSMContext):
    try:
        school_id = int(callback.data.rsplit(":", 1)[1])
        if school_id < 1:
            raise ValueError
    except (TypeError, ValueError):
        await callback.answer("شناسه مدرسه نامعتبر است.", show_alert=True)
        return
    await _show_school_management(callback, state, school_id)


async def _issue_school_group_link(
    callback: CallbackQuery,
    state: FSMContext,
    school_id: int,
    *,
    replace_existing: bool = False,
) -> None:
    if callback.message.chat.type != "private":
        await callback.answer("ساخت لینک اتصال فقط از پنل خصوصی انجام می‌شود.", show_alert=True)
        return
    try:
        bot_profile = await callback.bot.get_me()
    except Exception:
        await callback.answer("دریافت نام کاربری ربات ممکن نشد؛ دوباره تلاش کن.", show_alert=True)
        return
    if not bot_profile.username:
        await callback.answer("برای ربات نام کاربری تنظیم نشده است.", show_alert=True)
        return

    token = f"CM_{make_code()}"
    old_group_id = None
    async with SessionFactory() as session:
        async with session.begin():
            actor = await _user(session, callback.from_user, private=True)
            await lock_group_binding_mutations(session)
            school = await session.scalar(
                select(School).where(School.id == school_id).with_for_update()
            )
            if (
                not school
                or not school.is_active
                or not await is_school_admin(session, actor, school_id)
            ):
                await callback.answer("به مدیریت این مدرسه دسترسی نداری.", show_alert=True)
                return
            if school.telegram_group_id is not None and not replace_existing:
                await callback.answer(
                    "این مدرسه از قبل گروه مرکزی دارد؛ برای تغییر، تأیید جداگانه لازم است.",
                    show_alert=True,
                )
                return
            if replace_existing:
                if school.telegram_group_id is None:
                    await callback.answer("گروه مرکزی فعالی برای جایگزینی وجود ندارد.", show_alert=True)
                    return
                old_group_id = school.telegram_group_id
                school.telegram_group_id = None
                school.telegram_group_title = None
            school.group_link_token_hash = hash_code(token)
            school.group_link_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
            school_name = school.name

    if old_group_id is not None:
        try:
            await callback.bot.send_message(
                old_group_id,
                f"ℹ️ اتصال گروه مرکزی مدرسه «{escape(school_name)}» تغییر کرده است. "
                "این گروه دیگر مقصد اطلاع‌رسانی مدرسه نیست.",
            )
        except Exception:
            logger.info(
                "Could not notify old school group %s about rebinding",
                old_group_id,
                exc_info=True,
            )

    link = f"https://t.me/{bot_profile.username}?startgroup={token}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ افزودن ربات به گروه مدرسه", url=link)]
        ]
    )
    await callback.bot.send_message(
        callback.from_user.id,
        f"🔗 <b>لینک اتصال گروه مرکزی مدرسه {escape(school_name)}</b>\n"
        "این لینک یک‌بارمصرف است و تا ۲۴ ساعت اعتبار دارد. "
        "لینک را از داخل گروه مقصد اجرا کن و ربات را مدیر گروه قرار بده.",
        reply_markup=keyboard,
    )
    await _show_school_management(callback, state, school_id)


@router.callback_query(F.data.startswith("ui:school_group_link:"))
async def school_group_link(callback: CallbackQuery, state: FSMContext):
    try:
        school_id = int(callback.data.rsplit(":", 1)[1])
        if school_id < 1:
            raise ValueError
    except (TypeError, ValueError):
        await callback.answer("شناسه مدرسه نامعتبر است.", show_alert=True)
        return
    await _issue_school_group_link(callback, state, school_id)


@router.callback_query(F.data.startswith("ui:school_group_reset_confirm:"))
async def school_group_reset_confirm(callback: CallbackQuery, state: FSMContext):
    try:
        school_id = int(callback.data.rsplit(":", 1)[1])
        if school_id < 1:
            raise ValueError
    except (TypeError, ValueError):
        await callback.answer("شناسه مدرسه نامعتبر است.", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            actor = await _user(session, callback.from_user, private=True)
            school = await session.get(School, school_id)
            if (
                not school
                or not school.is_active
                or school.telegram_group_id is None
                or not await is_school_admin(session, actor, school_id)
            ):
                await callback.answer("گروه مرکزی یا مجوز مدیریت مدرسه معتبر نیست.", show_alert=True)
                return
            group_title = school.telegram_group_title or str(school.telegram_group_id)
    rows = [[
        _button("بله، تغییر گروه", f"ui:school_group_reset_do:{school_id}"),
        _button("خیر، برگشت", f"ui:school_manage:{school_id}"),
    ]]
    await _render_from_callback(
        callback,
        state,
        f"⚠️ گروه مرکزی فعلی «{escape(group_title)}» از مدرسه جدا می‌شود.\n"
        "پس از تأیید، لینک یک‌بارمصرف تازه برای اتصال گروه جایگزین صادر خواهد شد.",
        _markup(rows, f"ui:school_manage:{school_id}"),
    )


@router.callback_query(F.data.startswith("ui:school_group_reset_do:"))
async def school_group_reset_do(callback: CallbackQuery, state: FSMContext):
    try:
        school_id = int(callback.data.rsplit(":", 1)[1])
        if school_id < 1:
            raise ValueError
    except (TypeError, ValueError):
        await callback.answer("شناسه مدرسه نامعتبر است.", show_alert=True)
        return
    await _issue_school_group_link(callback, state, school_id, replace_existing=True)


@router.callback_query(F.data.startswith("ui:form:"))
async def form_panel(callback: CallbackQuery, state: FSMContext):
    try:
        parts = callback.data.split(":")
        if len(parts) not in {3, 4}:
            raise ValueError
        form_name = parts[2]
        context_id = int(parts[3]) if len(parts) == 4 else None
        if context_id is not None and context_id < 1:
            raise ValueError
    except (IndexError, TypeError, ValueError):
        await callback.answer("درخواست فرم نامعتبر است.", show_alert=True)
        return
    class_id = context_id if form_name in {
        "class_code", "approver_add", "subject_add", "timetable_set", "class_manager_add"
    } else None
    school_id = context_id if form_name in {"year_new", "class_new"} else None
    await _show_form(callback, state, form_name, class_id, school_id)


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
    await callback.answer("عملیات لغو شد.")
    if parent == "ui:platform":
        await _show_platform_panel(callback, state)
    elif parent == "ui:school":
        await _show_school_panel(callback, state)
    elif parent == "ui:classes":
        await _show_classes(callback, state, callback.from_user)
    elif parent.startswith("ui:class:"):
        await _show_class_panel(callback, state, int(parent.rsplit(":", 1)[1]))
    elif class_id:
        await _show_management_from_message(callback.message, state, callback.bot, class_id, callback.from_user)
    elif school_id:
        await _show_school_management(callback, state, int(school_id))
    else:
        await _close_panel(callback, state)


@router.callback_query(F.data.startswith("ui:review:"))
async def review_callback(callback: CallbackQuery, state: FSMContext):
    _, _, action, entity_type, entity_id_raw, class_id_raw = callback.data.split(":")
    entity_id, class_id = int(entity_id_raw), int(class_id_raw)
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            context = await get_class_with_school(session, class_id)
            if not context or not await can_approve(session, user, class_id):
                await callback.answer("مجوز بررسی این کلاس را نداری.", show_alert=True)
                return
            class_group, _, school = context
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
        if class_group.group_chat_id:
            try:
                await callback.bot.send_message(
                    class_group.group_chat_id,
                    f"اطلاعات {entity_type} با شناسه {entity_id} بررسی شد.",
                )
            except Exception:
                logger.exception("Failed to notify class group after review")
        await _show_pending_from_message(
            callback.message, state, callback.bot, class_id, callback.from_user
        )


@router.message(PanelInput.input, F.text.func(_is_panel_input_text))
async def panel_text_input(message: Message, state: FSMContext, bot: Bot):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    data = await state.get_data()
    if data.get("panel_form") == "search":
        await handle_search_input(message, state, bot, render_from_message=_render_from_message, markup=_markup, class_context=_class_context)
    else:
        await handle_panel_input(message, state, bot, render_from_message=_render_from_message, markup=_markup)
