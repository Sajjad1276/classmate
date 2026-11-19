from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from classmate.application.access import (
    class_role_for,
    get_class_scope,
    list_accessible_classes,
    require_class_access,
    school_admin_for,
)
from classmate.application.classrooms import (
    add_subject,
    assign_class_manager,
    issue_member_invite,
    join_class,
    list_subjects,
    set_timetable_slot,
)
from classmate.application.groups import (
    bind_class_group,
    get_bound_class,
    issue_class_group_link,
)
from classmate.application.identity import ensure_user
from classmate.application.records import (
    create_daily_report,
    create_exam,
    create_homework,
    cancel_record,
    get_daily_summary,
    list_exams,
    list_homework,
    list_pending,
    review_record,
)
from classmate.application.reminders import set_preference
from classmate.application.schools import (
    bind_school_group,
    assign_school_admin,
    create_academic_year,
    create_class,
    create_school,
    issue_school_group_link,
    list_managed_schools,
)
from classmate.bot.states import V2Flow
from classmate.core.config import get_settings
from classmate.db.models import (
    AcademicYear,
    ClassGroup,
    ClassMembership,
    Exam,
    Homework,
    NotificationPreference,
    School,
    SchoolMembership,
    Subject,
    User,
    TimetableEntry,
)
from classmate.db.session import SessionFactory
from classmate.domain.dates import (
    jalali_date,
    jalali_datetime,
    parse_school_date,
    parse_school_datetime,
)
from classmate.domain.enums import RecordType, Role
from classmate.domain.errors import DomainError, Forbidden
from classmate.domain.text import normalize_name, parse_positive_int
from classmate.presentation.telegram.formatting import html
from classmate.presentation.telegram.keyboards import (
    class_dashboard,
    classes_menu,
    main_keyboard,
    menu,
    record_types,
    reminder_preferences,
    subject_picker,
    button,
)

logger = logging.getLogger(__name__)
router = Router(name="classmate_v2")


async def _user(session, telegram_user, *, private: bool) -> User:
    settings = get_settings()
    return await ensure_user(
        session,
        telegram_id=telegram_user.id,
        full_name=telegram_user.full_name,
        username=telegram_user.username,
        private_chat_started=private,
        bootstrap_admin_ids=settings.bootstrap_admin_id_set,
    )


async def _role_flags(session, user: User) -> dict[str, bool]:
    school_admin = user.is_platform_admin or (
        await session.scalar(
            select(SchoolMembership.id).where(
                SchoolMembership.user_id == user.id,
                SchoolMembership.role == Role.SCHOOL_ADMIN.value,
                SchoolMembership.is_active.is_(True),
            )
        )
    ) is not None
    roles = set(
        (
            await session.scalars(
                select(ClassMembership.role).where(
                    ClassMembership.user_id == user.id,
                    ClassMembership.is_active.is_(True),
                )
            )
        ).all()
    )
    manager = Role.CLASS_MANAGER.value in roles
    approver = school_admin or manager or Role.TRUSTED_APPROVER.value in roles
    return {
        "platform": bool(user.is_platform_admin),
        "school": bool(school_admin),
        "manager": manager,
        "approver": bool(approver),
    }


async def _main_markup(user: User):
    async with SessionFactory() as session:
        async with session.begin():
            flags = await _role_flags(session, user)
    return main_keyboard(
        platform_admin=flags["platform"],
        school_admin=flags["school"],
        class_manager=flags["manager"],
        approver=flags["approver"],
    )


async def _edit(
    target: Message | CallbackQuery,
    state: FSMContext,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        return
    try:
        await message.edit_text(text, reply_markup=markup)
        await state.update_data(panel_message_id=message.message_id, panel_chat_id=message.chat.id)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            sent = await message.answer(text, reply_markup=markup)
            await state.update_data(panel_message_id=sent.message_id, panel_chat_id=sent.chat.id)
    if isinstance(target, CallbackQuery):
        await target.answer()


async def _send(
    message: Message,
    state: FSMContext,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    sent = await message.answer(text, reply_markup=markup)
    await state.update_data(panel_message_id=sent.message_id, panel_chat_id=sent.chat.id)


async def _return_home(message: Message, state: FSMContext, user: User) -> None:
    saved = await state.get_data()
    panel_id = saved.get("panel_message_id")
    panel_chat_id = saved.get("panel_chat_id", message.chat.id)
    markup = await _main_markup(user)
    await state.clear()
    text = "🏫 <b>ClassMate</b>\\nاز منوی زیر بخش موردنظر را انتخاب کن."
    if panel_id and panel_chat_id == message.chat.id:
        try:
            await message.bot.edit_message_text(
                text=text,
                chat_id=panel_chat_id,
                message_id=panel_id,
                reply_markup=markup,
            )
            await state.update_data(
                panel_message_id=panel_id,
                panel_chat_id=panel_chat_id,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                await state.update_data(
                    panel_message_id=panel_id,
                    panel_chat_id=panel_chat_id,
                )
                return
    await _send(message, state, text, markup)

async def _classes_for_user(user: User) -> list[ClassGroup]:
    async with SessionFactory() as session:
        async with session.begin():
            rows = await list_accessible_classes(session, user)
            return [row[0] for row in rows]


async def _load_user_from_message(message: Message, *, private: bool = True) -> User:
    async with SessionFactory() as session:
        async with session.begin():
            return await _user(session, message.from_user, private=private)


async def _show_class_selector(
    target: Message | CallbackQuery,
    state: FSMContext,
    user: User,
    action: str,
) -> None:
    classes = await _classes_for_user(user)
    if action == "pending":
        visible = []
        for item in classes:
            async with SessionFactory() as session:
                async with session.begin():
                    role = await class_role_for(session, user, item.id)
                    _, _, school = await get_class_scope(session, item.id)
                    allowed = (
                        user.is_platform_admin
                        or await school_admin_for(session, user, school.id)
                        or role in {Role.CLASS_MANAGER.value, Role.TRUSTED_APPROVER.value}
                    )
            if allowed:
                visible.append(item)
        classes = visible
    elif action == "manage":
        visible = []
        for item in classes:
            async with SessionFactory() as session:
                async with session.begin():
                    _, _, school = await get_class_scope(session, item.id)
                    role = await class_role_for(session, user, item.id)
                    allowed = (
                        user.is_platform_admin
                        or await school_admin_for(session, user, school.id)
                        or role == Role.CLASS_MANAGER.value
                    )
            if allowed:
                visible.append(item)
        classes = visible
    if not classes:
        message = (
            "برای بررسی این کلاس‌ها مجوز نداری."
            if action == "pending"
            else "کلاس مدیریتی برای این حساب پیدا نشد."
            if action == "manage"
            else "هنوز به کلاسی متصل نیستی. از مدیر کلاس کد دعوت بگیر."
        )
        await _edit(target, state, message, menu([]))
        return
    if len(classes) == 1:
        if action == "open":
            await _show_class_dashboard(target, state, user, classes[0].id)
        elif action == "new":
            await _edit(target, state, "چه نوع اطلاعاتی می‌خواهی ثبت کنی؟", record_types(classes[0].id))
        else:
            await _show_class_action(target, state, user, action, classes[0].id)
        return
    await _edit(
        target,
        state,
        "کلاس موردنظر را انتخاب کن:",
        classes_menu(classes, action),
    )


async def _show_class_dashboard(
    target: Message | CallbackQuery,
    state: FSMContext,
    user: User,
    class_id: int,
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            class_group, _, school = await require_class_access(session, user, class_id)
            role = await class_role_for(session, user, class_id)
            can_manage = (
                user.is_platform_admin
                or await school_admin_for(session, user, school.id)
                or role == Role.CLASS_MANAGER.value
            )
            can_review = (
                can_manage
                or role == Role.TRUSTED_APPROVER.value
            )
    await _edit(
        target,
        state,
        f"🏫 <b>{html(class_group.name)}</b>\n"
        f"مدرسه: {html(school.name)}\n"
        "یک عملیات را انتخاب کن:",
        class_dashboard(class_id, can_review=can_review, can_manage=can_manage),
    )


def _format_homework(rows: list[tuple[Homework, Subject]]) -> str:
    if not rows:
        return "📚 تکلیف تأییدشده‌ای ثبت نشده است."
    lines = ["📚 <b>تکالیف تأییدشده</b>"]
    for item, subject in rows[:8]:
        due = jalali_date(item.due_on) if item.due_on else "بدون موعد"
        desc = html((item.description or "")[:180])
        lines.append(
            f"\n• <b>{html(subject.name)}</b> — {html(item.title)}\n"
            f"{desc}\nموعد: {due}"
        )
    if len(rows) > 8:
        lines.append(f"\n{len(rows) - 8} مورد دیگر نمایش داده نشد.")
    return "\n".join(lines)


def _format_exams(rows: list[tuple[Exam, Subject]], timezone_name: str) -> str:
    if not rows:
        return "📝 امتحان آینده‌ای ثبت نشده است."
    lines = ["📝 <b>امتحانات آینده</b>"]
    for item, subject in rows[:8]:
        lines.append(
            f"\n• <b>{html(subject.name)}</b> — {html(item.title)}\n"
            f"{html(item.scope[:160])}\n"
            f"{jalali_datetime(item.scheduled_at, timezone_name)}"
        )
    return "\n".join(lines)


def _format_today(summary: dict[str, list], today, timezone_name: str) -> str:
    parts = [f"🗒 <b>خلاصه امروز — {jalali_date(today)}</b>"]
    reports = summary["reports"]
    homework = summary["homework"]
    exams = summary["exams"]
    if reports:
        for item in reports[:3]:
            parts.append(f"📌 <b>{html(item.title or 'گزارش روزانه')}</b>\n{html(item.body[:220])}")
    else:
        parts.append("گزارش تأییدشده‌ای ثبت نشده است.")
    if homework:
        parts.append("📚 <b>تکالیف امروز</b>")
        for item, subject in homework[:4]:
            parts.append(f"• {html(subject.name)}: {html(item.title)}")
    else:
        parts.append("امروز تکلیف موعدداری نیست.")
    if exams:
        parts.append("📝 <b>امتحانات امروز</b>")
        for item, subject in exams[:4]:
            parts.append(
                f"• {html(subject.name)}: {html(item.title)} "
                f"({jalali_datetime(item.scheduled_at, timezone_name)})"
            )
    else:
        parts.append("امروز امتحانی نیست.")
    result = "\n\n".join(parts)
    return result[:3900]


async def _show_class_action(
    target: Message | CallbackQuery,
    state: FSMContext,
    user: User,
    action: str,
    class_id: int,
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            class_group, _, school = await require_class_access(session, user, class_id)
            if action == "homework":
                rows = await list_homework(session, user, class_id)
                body = _format_homework(rows)
            elif action == "exams":
                rows = await list_exams(session, user, class_id)
                body = _format_exams(rows, school.timezone)
            elif action == "today":
                today = datetime.now(ZoneInfo(school.timezone)).date()
                summary = await get_daily_summary(
                    session, user, class_id, today, school.timezone
                )
                body = _format_today(summary, today, school.timezone)
            elif action == "week":
                today = datetime.now(ZoneInfo(school.timezone)).date()
                rows = list(
                    (
                        await session.execute(
                            select(TimetableEntry, Subject)
                            .join(Subject, Subject.id == TimetableEntry.subject_id)
                            .where(
                                TimetableEntry.class_id == class_id,
                                (TimetableEntry.valid_from.is_(None))
                                | (TimetableEntry.valid_from <= today),
                                (TimetableEntry.valid_until.is_(None))
                                | (TimetableEntry.valid_until >= today),
                            )
                            .order_by(TimetableEntry.weekday, TimetableEntry.period)
                        )
                    ).all()
                )
                body = (
                    "برنامه هفتگی هنوز ثبت نشده است."
                    if not rows
                    else "📅 <b>برنامه هفتگی</b>\n"
                    + "\n".join(
                        f"{['شنبه','یکشنبه','دوشنبه','سه‌شنبه','چهارشنبه','پنجشنبه','جمعه'][slot.weekday]} "
                        f"— زنگ {slot.period}: {html(subject.name)}"
                        for slot, subject in rows
                    )
                )
            elif action == "reminders":
                pref = await session.scalar(
                    select(NotificationPreference).where(
                        NotificationPreference.user_id == user.id,
                        NotificationPreference.class_id == class_id,
                    )
                )
                body = (
                    "🔔 <b>یادآوری‌های خصوصی</b>\n"
                    "روشن یا خاموش‌کردن هر دسته فقط روی پیام خصوصی خودت اثر دارد."
                )
                await _edit(
                    target,
                    state,
                    body,
                    reminder_preferences(
                        class_id,
                        pref.homework_enabled if pref else True,
                        pref.exams_enabled if pref else True,
                    ),
                )
                return
            elif action == "pending":
                pending = await list_pending(session, user, class_id)
                if not pending:
                    body = "🕓 موردی در انتظار بررسی نیست."
                    markup = menu([], f"v2:class:open:{class_id}")
                else:
                    parts = ["🕓 <b>صف بررسی</b>"]
                    rows = []
                    for kind, record in pending[:10]:
                        label = {
                            RecordType.HOMEWORK: "تکلیف",
                            RecordType.EXAM: "امتحان",
                            RecordType.REPORT: "گزارش",
                        }[kind]
                        title = getattr(record, "title", None) or "گزارش روزانه"
                        parts.append(f"\n• {label}: {html(title)}")
                        rows.append(
                            [
                                button("✅ تأیید", f"v2:review:approve:{kind.value}:{class_id}:{record.id}"),
                                button("❌ رد", f"v2:review:reject:{kind.value}:{class_id}:{record.id}"),
                            ]
                        )
                    body = "\n".join(parts)
                    markup = menu(rows, f"v2:class:open:{class_id}")
                await _edit(target, state, body, markup)
                return
            elif action == "manage":
                role = await class_role_for(session, user, class_id)
                if not (
                    user.is_platform_admin
                    or await school_admin_for(session, user, school.id)
                    or role == Role.CLASS_MANAGER.value
                ):
                    raise Forbidden("دسترسی مدیریت این کلاس را نداری.")
                rows = [
                    [button("🔑 ساخت کد عضویت", f"v2:manage:invite:{class_id}")],
                    [button("🔗 اتصال گروه کلاس", f"v2:manage:group:{class_id}")],
                    [button("📘 افزودن درس", f"v2:manage:subject:{class_id}")],
                ]
                body = f"⚙️ <b>مدیریت {html(class_group.name)}</b>\nعملیات مدیریتی را انتخاب کن."
                await _edit(target, state, body, menu(rows, f"v2:class:open:{class_id}"))
                return
            elif action == "search":
                await _begin_flow(
                    target, state, flow="search", step="query", class_id=class_id,
                    prompt="عبارت جست‌وجو را بنویس. فقط اطلاعات تأییدشده بررسی می‌شوند.",
                    back=f"v2:class:open:{class_id}",
                )
                return
            else:
                body = "این بخش هنوز در نسخهٔ بازنویسی‌شده فعال نشده است."
    await _edit(target, state, body, menu([], f"v2:class:open:{class_id}"))


async def _begin_flow(
    target: Message | CallbackQuery,
    state: FSMContext,
    *,
    flow: str,
    step: str,
    prompt: str,
    back: str = "v2:home",
    **data,
) -> None:
    message = target.message if isinstance(target, CallbackQuery) else target
    await state.set_state(V2Flow.input)
    await state.update_data(
        flow=flow,
        step=step,
        panel_message_id=message.message_id,
        panel_chat_id=message.chat.id,
        **data,
    )
    await _edit(target, state, f"✍️ {prompt}", InlineKeyboardMarkup(
        inline_keyboard=[[button("❌ لغو فرم", "v2:form:cancel")]]
    ))


async def _finish_flow(
    message: Message,
    state: FSMContext,
    text: str,
    *,
    class_id: int | None = None,
) -> None:
    data = await state.get_data()
    panel_id = data.get("panel_message_id")
    chat_id = data.get("panel_chat_id", message.chat.id)
    await state.clear()
    if class_id:
        async with SessionFactory() as session:
            async with session.begin():
                user = await _user(
                    session, message.from_user, private=message.chat.type == "private"
                )
                class_group, _, school = await require_class_access(session, user, class_id)
                role = await class_role_for(session, user, class_id)
                can_manage = (
                    user.is_platform_admin
                    or await school_admin_for(session, user, school.id)
                    or role == Role.CLASS_MANAGER.value
                )
                can_review = can_manage or role == Role.TRUSTED_APPROVER.value
                group_mode = message.chat.type in {"group", "supergroup"}
        markup = class_dashboard(
            class_id,
            can_review=can_review,
            can_manage=can_manage,
            group_mode=group_mode,
        )
    else:
        markup = await _home_markup_for_telegram(message)
    if panel_id:
        try:
            await message.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=panel_id,
                reply_markup=markup,
            )
            return
        except TelegramBadRequest:
            pass
    await message.answer(text, reply_markup=markup)


async def _finish_school_flow(
    message: Message,
    state: FSMContext,
    text: str,
    school_id: int,
) -> None:
    data = await state.get_data()
    panel_id = data.get("panel_message_id")
    chat_id = data.get("panel_chat_id", message.chat.id)
    await state.clear()
    markup = menu([], f"v2:school:open:{school_id}")
    if panel_id:
        try:
            await message.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=panel_id,
                reply_markup=markup,
            )
            await state.update_data(panel_message_id=panel_id, panel_chat_id=chat_id)
            return
        except TelegramBadRequest:
            pass
    await _send(message, state, text, markup)


async def _home_markup_for_telegram(message: Message):
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            flags = await _role_flags(session, user)
    return main_keyboard(
        platform_admin=flags["platform"],
        school_admin=flags["school"],
        class_manager=flags["manager"],
        approver=flags["approver"],
    )


async def _send_pending_notice(bot: Bot, class_id: int, text: str) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            class_group = await session.get(ClassGroup, class_id)
            group_id = class_group.group_chat_id if class_group else None
    if group_id:
        try:
            await bot.send_message(group_id, text)
        except TelegramAPIError:
            logger.exception("Unable to notify class group after a new submission.")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    argument = message.text.split(maxsplit=1)[1].strip() if len(message.text.split(maxsplit=1)) > 1 else ""
    if message.chat.type in {"group", "supergroup"}:
        if argument.startswith(("CL_", "SC_")):
            try:
                bot_user = await bot.get_me()
                bot_member = await bot.get_chat_member(message.chat.id, bot_user.id)
                actor_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
                if bot_member.status not in {"administrator", "creator"}:
                    await message.answer("برای اتصال، ابتدا ربات را در گروه مدیر کن.")
                    return
                if actor_member.status not in {"administrator", "creator"}:
                    await message.answer("فقط مدیر گروه می‌تواند اتصال را انجام دهد.")
                    return
                async with SessionFactory() as session:
                    async with session.begin():
                        if argument.startswith("CL_"):
                            item = await bind_class_group(
                                session,
                                code=argument[3:],
                                chat_id=message.chat.id,
                                chat_title=message.chat.title or "",
                            )
                            title = f"کلاس {item.name}"
                        else:
                            item = await bind_school_group(
                                session,
                                code=argument[3:],
                                chat_id=message.chat.id,
                                chat_title=message.chat.title or "",
                            )
                            title = f"مدرسه {item.name}"
                await message.answer(f"✅ گروه با موفقیت به {html(title)} متصل شد.")
                return
            except DomainError as exc:
                await message.answer(f"⚠️ {html(str(exc))}")
                return
        bound_context = None
        async with SessionFactory() as session:
            async with session.begin():
                bound = await get_bound_class(session, message.chat.id)
                if bound:
                    class_group, school = bound
                    group_user = await _user(session, message.from_user, private=False)
                    await session.execute(
                        pg_insert(ClassMembership).values(
                            class_id=class_group.id,
                            user_id=group_user.id,
                            role=Role.STUDENT.value,
                            is_active=True,
                        ).on_conflict_do_nothing(
                            constraint="uq_class_user"
                        )
                    )
                    role = await class_role_for(session, group_user, class_group.id)
                    can_review = (
                        group_user.is_platform_admin
                        or await school_admin_for(session, group_user, school.id)
                        or role in {
                            Role.CLASS_MANAGER.value,
                            Role.TRUSTED_APPROVER.value,
                        }
                    )
                    bound_context = (class_group, school, can_review)
                school_name = None
                if bound_context is None:
                    school_name = await session.scalar(
                        select(School.name).where(School.telegram_group_id == message.chat.id)
                    )
        if bound_context:
            class_group, school, can_review = bound_context
            await message.answer(
                f"🏫 <b>{html(class_group.name)}</b> — {html(school.name)}",
                reply_markup=class_dashboard(
                    class_group.id,
                    can_review=can_review,
                    can_manage=False,
                    group_mode=True,
                ),
            )
            return
        if school_name:
            await message.answer(f"🏢 گروه مرکزی مدرسه {html(school_name)}")
        else:
            await message.answer("این گروه هنوز به ClassMate متصل نشده است.")
        return

    if argument.startswith("join_"):
        async with SessionFactory() as session:
            async with session.begin():
                user = await _user(session, message.from_user, private=True)
                item = await join_class(session, user, argument[5:])
        await _return_home(message, state, user)
        await message.answer(f"✅ به کلاس «{html(item.name)}» اضافه شد.")
        return

    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=True)
    await _return_home(message, state, user)


@router.message(Command("help"))
async def help_command(message: Message, state: FSMContext) -> None:
    await _load_user_from_message(message)
    await state.clear()
    await _send(
        message,
        state,
        "ℹ️ <b>راهنمای ClassMate</b>\n"
        "از دکمه‌های همین پنل استفاده کن. اطلاعات ابتدا در انتظار بررسی‌اند و فقط پس از تأیید در فهرست قطعی نمایش داده می‌شوند.",
        await _home_markup_for_telegram(message),
    )


@router.callback_query(F.data == "v2:home")
async def home_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            flags = await _role_flags(session, user)
    await _edit(
        callback,
        state,
        "🏫 <b>ClassMate</b>\nاز منوی زیر بخش موردنظر را انتخاب کن.",
        main_keyboard(
            platform_admin=flags["platform"],
            school_admin=flags["school"],
            class_manager=flags["manager"],
            approver=flags["approver"],
        ),
    )


@router.callback_query(F.data == "v2:join")
async def join_start_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await _begin_flow(
        callback,
        state,
        flow="join",
        step="code",
        prompt="کد دعوت کلاس را وارد کن. کد را از مدیر کلاس بگیر.",
        back="v2:home",
    )


@router.callback_query(F.data.startswith("v2:classes:"))
async def classes_callback(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[2]
    user = await _load_user_from_callback(callback)
    await _show_class_selector(callback, state, user, action)


async def _load_user_from_callback(callback: CallbackQuery) -> User:
    async with SessionFactory() as session:
        async with session.begin():
            return await _user(session, callback.from_user, private=True)


@router.callback_query(F.data.startswith("v2:class:"))
async def class_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, action, class_id_raw = callback.data.split(":")
        class_id = int(class_id_raw)
        if class_id < 1:
            raise ValueError
    except (ValueError, TypeError):
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    try:
        if action == "open":
            await _show_class_dashboard(callback, state, user, class_id)
        elif action == "new":
            await _edit(callback, state, "چه اطلاعاتی می‌خواهی ثبت کنی؟", record_types(class_id))
        else:
            await _show_class_action(callback, state, user, action, class_id)
    except DomainError as exc:
        await _edit(callback, state, f"⚠️ {html(str(exc))}", menu([]))


@router.callback_query(F.data.startswith("v2:action:"))
async def class_action_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, action, class_id_raw = callback.data.split(":")
        class_id = int(class_id_raw)
    except (ValueError, TypeError):
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    try:
        await _show_class_action(callback, state, user, action, class_id)
    except DomainError as exc:
        await _edit(callback, state, f"⚠️ {html(str(exc))}", menu([]))


@router.callback_query(F.data.startswith("v2:new:"))
async def new_record_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        class_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("شناسه کلاس معتبر نیست.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    try:
        async with SessionFactory() as session:
            async with session.begin():
                await require_class_access(session, user, class_id)
        await _edit(callback, state, "چه اطلاعاتی می‌خواهی ثبت کنی؟", record_types(class_id))
    except DomainError as exc:
        await callback.answer(str(exc), show_alert=True)


@router.callback_query(F.data.startswith("v2:record:"))
async def record_start_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, kind, class_id_raw = callback.data.split(":")
        class_id = int(class_id_raw)
        record_type = RecordType(kind)
    except (ValueError, TypeError):
        await callback.answer("نوع ثبت نامعتبر است.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    try:
        async with SessionFactory() as session:
            async with session.begin():
                await require_class_access(session, user, class_id)
                if record_type in {RecordType.HOMEWORK, RecordType.EXAM}:
                    subjects = await list_subjects(session, user, class_id)
                else:
                    subjects = []
        if record_type == RecordType.REPORT:
            await _begin_flow(
                callback,
                state,
                flow="record",
                step="report_body",
                prompt="گزارش امروز را بنویس. هر ثبت یک گزارش مستقل و در انتظار بررسی خواهد بود.",
                class_id=class_id,
                record_type=record_type.value,
                subject_id=None,
            )
        elif not subjects:
            await _edit(
                callback,
                state,
                "برای این کلاس هنوز درسی ثبت نشده است. مدیر کلاس باید ابتدا درس‌ها را اضافه کند.",
                menu([], f"v2:class:open:{class_id}"),
            )
        else:
            await _edit(
                callback,
                state,
                "درس مربوط به این اطلاعات را انتخاب کن:",
                subject_picker(subjects, record_type.value, class_id),
            )
    except DomainError as exc:
        await callback.answer(str(exc), show_alert=True)


@router.callback_query(F.data.startswith("v2:subject:"))
async def subject_selected_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, kind, class_id_raw, subject_id_raw = callback.data.split(":")
        class_id, subject_id = int(class_id_raw), int(subject_id_raw)
        record_type = RecordType(kind)
        if record_type == RecordType.REPORT:
            raise ValueError
    except (ValueError, TypeError):
        await callback.answer("انتخاب درس معتبر نیست.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    try:
        async with SessionFactory() as session:
            async with session.begin():
                subjects = await list_subjects(session, user, class_id)
                if subject_id not in {item.id for item in subjects}:
                    raise Forbidden("این درس متعلق به کلاس انتخاب‌شده نیست.")
        await _begin_flow(
            callback,
            state,
            flow="record",
            step="title",
            prompt="عنوان را کوتاه و دقیق وارد کن.",
            class_id=class_id,
            record_type=record_type.value,
            subject_id=subject_id,
        )
    except DomainError as exc:
        await callback.answer(str(exc), show_alert=True)


@router.callback_query(F.data.startswith("v2:reminder:"))
async def reminder_toggle_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, category, class_id_raw, value_raw = callback.data.split(":")
        class_id = int(class_id_raw)
        enabled = bool(int(value_raw))
        if category not in {"homework", "exams"} or value_raw not in {"0", "1"}:
            raise ValueError
    except (ValueError, TypeError):
        await callback.answer("تنظیم نامعتبر است.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    try:
        async with SessionFactory() as session:
            async with session.begin():
                class_group, _, school = await require_class_access(session, user, class_id)
                await set_preference(
                    session,
                    user=user,
                    class_group=class_group,
                    category=category,
                    enabled=enabled,
                    timezone_name=school.timezone,
                )
        await callback.answer("تنظیم یادآوری ذخیره شد.")
        await _show_class_action(callback, state, user, "reminders", class_id)
    except DomainError as exc:
        await callback.answer(str(exc), show_alert=True)


@router.callback_query(F.data.startswith("v2:review:"))
async def review_callback(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    try:
        _, _, decision, kind, class_id_raw, record_id_raw = callback.data.split(":")
        class_id, record_id = int(class_id_raw), int(record_id_raw)
        record_type = RecordType(kind)
        if decision not in {"approve", "reject"}:
            raise ValueError
    except (ValueError, TypeError):
        await callback.answer("درخواست بررسی معتبر نیست.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    submitter_telegram_id = None
    group_id = None
    try:
        async with SessionFactory() as session:
            async with session.begin():
                record = await review_record(
                    session,
                    user,
                    class_id,
                    record_type,
                    record_id,
                    approve=(decision == "approve"),
                )
                submitter = await session.get(User, record.created_by)
                if submitter and submitter.private_chat_started:
                    submitter_telegram_id = submitter.telegram_id
                class_group = await session.get(ClassGroup, class_id)
                group_id = class_group.group_chat_id if class_group else None
        label = {
            RecordType.HOMEWORK: "تکلیف",
            RecordType.EXAM: "امتحان",
            RecordType.REPORT: "گزارش",
        }[record_type]
        if submitter_telegram_id:
            try:
                await bot.send_message(
                    submitter_telegram_id,
                    f"{'✅' if decision == 'approve' else '❌'} {label} ثبت‌شدهٔ تو "
                    f"{'تأیید شد' if decision == 'approve' else 'رد شد'}.",
                )
            except TelegramAPIError:
                logger.info("Private review notification could not be delivered.", exc_info=True)
        if group_id:
            try:
                await bot.send_message(
                    group_id,
                    f"{'✅' if decision == 'approve' else '❌'} {label} ثبت‌شده بررسی شد.",
                )
            except TelegramAPIError:
                logger.info("Group review notification could not be delivered.", exc_info=True)
        await callback.answer("تصمیم ثبت شد.")
        await _show_class_action(callback, state, user, "pending", class_id)
    except DomainError as exc:
        await callback.answer(str(exc), show_alert=True)


@router.callback_query(F.data.startswith("v2:manage:"))
async def manage_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, action, class_id_raw = callback.data.split(":")
        class_id = int(class_id_raw)
    except (ValueError, TypeError):
        await callback.answer("درخواست مدیریتی معتبر نیست.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    try:
        if action == "invite":
            async with SessionFactory() as session:
                async with session.begin():
                    code = await issue_member_invite(session, user, class_id)
            await _edit(
                callback, state,
                f"🔑 <b>کد عضویت کلاس</b>\n<code>{html(code)}</code>\nاعتبار: ۷ روز",
                menu([], f"v2:class:open:{class_id}"),
            )
        elif action == "group":
            async with SessionFactory() as session:
                async with session.begin():
                    code = await issue_class_group_link(session, user, class_id)
            me = await callback.bot.get_me()
            link = f"https://t.me/{me.username}?startgroup=CL_{code}"
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [button("➕ افزودن ربات به گروه کلاس", link)],
                    [button("⬅️ بازگشت", f"v2:class:open:{class_id}")],
                ]
            )
            await _edit(
                callback, state,
                "🔗 لینک اتصال ۳۰ دقیقه اعتبار دارد. ربات را در گروه مدیر کن و لینک را از همان گروه باز کن.",
                markup,
            )
        elif action == "subject":
            await _begin_flow(
                callback,
                state,
                flow="subject_add",
                step="name",
                prompt="نام درس را وارد کن.",
                class_id=class_id,
                back=f"v2:class:open:{class_id}",
            )
        else:
            await callback.answer("این عملیات شناخته‌شده نیست.", show_alert=True)
    except DomainError as exc:
        await callback.answer(str(exc), show_alert=True)


@router.callback_query(F.data == "v2:schools:manage")
async def schools_manage_callback(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _load_user_from_callback(callback)
    async with SessionFactory() as session:
        async with session.begin():
            schools = await list_managed_schools(session, user)
    rows = [
        [button(f"🏢 {school.name[:42]}", f"v2:school:open:{school.id}")]
        for school in schools
    ]
    if user.is_platform_admin:
        rows.insert(0, [button("➕ ایجاد مدرسه و سال", "v2:school:create")])
    await _edit(callback, state, "🏢 <b>مدیریت مدرسه</b>\nمدرسه را انتخاب کن:", menu(rows))


@router.callback_query(F.data == "v2:platform")
async def platform_callback(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _load_user_from_callback(callback)
    if not user.is_platform_admin:
        await callback.answer("این پنل فقط برای مدیر کل است.", show_alert=True)
        return
    rows = [
        [button("➕ ایجاد مدرسه و سال تحصیلی", "v2:school:create")],
        [button("🏢 فهرست مدرسه‌ها", "v2:schools:manage")],
    ]
    await _edit(callback, state, "🛡 <b>پنل مدیر کل</b>", menu(rows))


@router.callback_query(
    (F.data == "v2:school:create") | F.data.startswith("v2:school:open:")
)
async def school_callback(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    user = await _load_user_from_callback(callback)
    if len(parts) == 3 and parts[2] == "create":
        if not user.is_platform_admin:
            await callback.answer("فقط مدیر کل می‌تواند مدرسه ایجاد کند.", show_alert=True)
            return
        await _begin_flow(
            callback, state, flow="school_create", step="name",
            prompt="نام مدرسه را وارد کن.", back="v2:platform",
        )
        return
    try:
        if len(parts) != 4 or parts[2] != "open":
            raise ValueError
        school_id = int(parts[3])
    except ValueError:
        await callback.answer("شناسه مدرسه معتبر نیست.", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            school = await session.get(School, school_id)
            if school is None or not await school_admin_for(session, user, school_id):
                await callback.answer("به این مدرسه دسترسی نداری.", show_alert=True)
                return
            years = list(
                (
                    await session.scalars(
                        select(AcademicYear)
                        .where(AcademicYear.school_id == school_id)
                        .order_by(AcademicYear.is_current.desc(), AcademicYear.name.desc())
                    )
                ).all()
            )
    rows = [
        [button("➕ ایجاد سال تحصیلی", f"v2:school:create_year:{school_id}")],
        [button("➕ تعیین مدیر مدرسه", f"v2:school:assign_admin:{school_id}")]
        if user.is_platform_admin else [],
    ]
    rows = [row for row in rows if row]
    if years:
        rows.extend([
            [button(
                f"🏫 ساخت کلاس در {year.name}{' · جاری' if year.is_current else ''}",
                f"v2:school:create_class:{school_id}:{year.id}",
            )]
            for year in years
        ])
    rows.append([button("🔗 اتصال گروه مرکزی", f"v2:school:grouplink:{school_id}")])
    await _edit(
        callback,
        state,
        f"🏢 <b>{html(school.name)}</b>\n"
        f"منطقه زمانی: {html(school.timezone)}\n"
        f"تعداد سال‌های تحصیلی: {len(years)}",
        menu(rows, "v2:schools:manage"),
    )


@router.callback_query(F.data.startswith("v2:school:create_year:"))
async def school_create_year_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        school_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("مدرسه معتبر نیست.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    async with SessionFactory() as session:
        async with session.begin():
            school = await session.get(School, school_id)
            allowed = bool(school and await school_admin_for(session, user, school_id))
    if not allowed:
        await callback.answer("مجوز مدیریت این مدرسه را نداری.", show_alert=True)
        return
    await _begin_flow(
        callback, state, flow="academic_year_create", step="name",
        prompt="نام سال تحصیلی جدید را وارد کن؛ مثلاً ۱۴۰۶-۱۴۰۷.",
        school_id=school_id,
        back=f"v2:school:open:{school_id}",
    )


@router.callback_query(F.data.startswith("v2:school:assign_admin:"))
async def school_assign_admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        school_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("مدرسه معتبر نیست.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    if not user.is_platform_admin:
        await callback.answer("فقط مدیر کل می‌تواند مدیر مدرسه تعیین کند.", show_alert=True)
        return
    await _begin_flow(
        callback, state, flow="school_admin_assign", step="telegram_id",
        prompt="شناسه عددی تلگرام مدیر جدید را وارد کن. او باید قبلاً ربات را Start کرده باشد.",
        school_id=school_id,
        back=f"v2:school:open:{school_id}",
    )


@router.callback_query(F.data.startswith("v2:school:create_class:"))
async def school_create_class_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        parts = callback.data.split(":")
        if len(parts) == 5:
            school_id, year_id = int(parts[3]), int(parts[4])
        elif len(parts) == 4:
            school_id = int(parts[3])
            year_id = 0
        else:
            raise ValueError
    except ValueError:
        await callback.answer("مدرسه یا سال تحصیلی معتبر نیست.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    async with SessionFactory() as session:
        async with session.begin():
            if year_id:
                year = await session.scalar(
                    select(AcademicYear).where(
                        AcademicYear.id == year_id,
                        AcademicYear.school_id == school_id,
                    )
                )
            else:
                year = await session.scalar(
                    select(AcademicYear).where(
                        AcademicYear.school_id == school_id,
                        AcademicYear.is_current.is_(True),
                    )
                )
            school_admin = await school_admin_for(session, user, school_id)
    if year is None or not school_admin:
        await callback.answer("سال تحصیلی یا مجوز مدیریت مدرسه پیدا نشد.", show_alert=True)
        return
    await _begin_flow(
        callback,
        state,
        flow="class_create",
        step="grade",
        prompt="پایه را وارد کن؛ مثلاً دهم.",
        school_id=school_id,
        academic_year_id=year.id,
        back=f"v2:school:open:{school_id}",
    )


@router.callback_query(F.data.startswith("v2:school:grouplink:"))
async def school_group_link_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        school_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("مدرسه معتبر نیست.", show_alert=True)
        return
    user = await _load_user_from_callback(callback)
    try:
        async with SessionFactory() as session:
            async with session.begin():
                code = await issue_school_group_link(session, user, school_id)
        me = await callback.bot.get_me()
        link = f"https://t.me/{me.username}?startgroup=SC_{code}"
        await _edit(
            callback,
            state,
            "🔗 لینک یک‌بارمصرف اتصال گروه مرکزی تا ۲۴ ساعت معتبر است. ربات باید در گروه مدیر باشد.",
            InlineKeyboardMarkup(inline_keyboard=[
                [button("➕ افزودن ربات به گروه مدرسه", link)],
                [button("⬅️ بازگشت", f"v2:school:open:{school_id}")],
            ]),
        )
    except DomainError as exc:
        await callback.answer(str(exc), show_alert=True)


@router.message(V2Flow.input, F.text, ~F.text.startswith("/"))
async def form_input(message: Message, state: FSMContext, bot: Bot) -> None:
    value = (message.text or "").strip()
    data = await state.get_data()
    flow = data.get("flow")
    step = data.get("step")
    user = await _load_user_from_message(message, private=message.chat.type == "private")
    try:
        if flow == "join":
            if step != "code":
                raise DomainError("مرحله عضویت نامعتبر است؛ دوباره شروع کن.")
            async with SessionFactory() as session:
                async with session.begin():
                    class_group = await join_class(session, user, value)
            await _finish_flow(message, state, f"✅ به کلاس «{html(class_group.name)}» اضافه شد.")
            return

        if flow == "school_create":
            if step == "name":
                clean = normalize_name(value, maximum=180)
                await state.update_data(school_name=clean, step="academic_year")
                await _edit_saved_prompt(
                    message, state, "نام سال تحصیلی را وارد کن؛ مثلاً ۱۴۰۵-۱۴۰۶."
                )
                return
            if step == "academic_year":
                async with SessionFactory() as session:
                    async with session.begin():
                        school, year = await create_school(
                            session,
                            user,
                            name=data["school_name"],
                            academic_year=value,
                            timezone_name=get_settings().default_timezone,
                        )
                await _finish_flow(
                    message, state,
                    f"✅ مدرسه «{html(school.name)}» و سال «{html(year.name)}» ساخته شد.",
                )
                return

        if flow == "academic_year_create":
            if step != "name":
                raise DomainError("مرحله ساخت سال تحصیلی نامعتبر است.")
            async with SessionFactory() as session:
                async with session.begin():
                    year = await create_academic_year(
                        session,
                        user,
                        school_id=int(data["school_id"]),
                        name=value,
                    )
            await _finish_school_flow(
                message,
                state,
                f"✅ سال تحصیلی «{html(year.name)}» ساخته و فعال شد.",
                int(data["school_id"]),
            )
            return

        if flow == "school_admin_assign":
            if step != "telegram_id":
                raise DomainError("مرحله تعیین مدیر مدرسه نامعتبر است.")
            telegram_id = parse_positive_int(value)
            async with SessionFactory() as session:
                async with session.begin():
                    if not user.is_platform_admin:
                        raise Forbidden("فقط مدیر کل می‌تواند مدیر مدرسه تعیین کند.")
                    target = await session.scalar(
                        select(User).where(
                            User.telegram_id == telegram_id,
                            User.is_active.is_(True),
                        )
                    )
                    if target is None or not target.private_chat_started:
                        raise DomainError("این کاربر هنوز ربات را در گفت‌وگوی خصوصی Start نکرده است.")
                    membership = await assign_school_admin(
                        session,
                        user,
                        school_id=int(data["school_id"]),
                        target=target,
                    )
                    school = await session.get(School, int(data["school_id"]))
            await _finish_school_flow(
                message,
                state,
                f"✅ {html(target.full_name)} مدیر مدرسه «{html(school.name)}» شد.",
                int(data["school_id"]),
            )
            return

        if flow == "class_create":
            if step == "grade":
                await state.update_data(grade=normalize_name(value, maximum=32), step="name")
                await _edit_saved_prompt(message, state, "نام کلاس را وارد کن؛ مثلاً دهم تجربی ۱.")
                return
            if step == "name":
                await state.update_data(class_name=normalize_name(value, maximum=80), step="section")
                await _edit_saved_prompt(message, state, "نام شعبه را وارد کن؛ اگر ندارد «-» بفرست.")
                return
            if step == "section":
                async with SessionFactory() as session:
                    async with session.begin():
                        item = await create_class(
                            session,
                            user,
                            school_id=int(data["school_id"]),
                            academic_year_id=int(data["academic_year_id"]),
                            name=data["class_name"],
                            grade=data["grade"],
                            section=None if value == "-" else value,
                        )
                await _finish_flow(message, state, f"✅ کلاس «{html(item.name)}» ساخته شد.")
                return

        if flow == "subject_add":
            async with SessionFactory() as session:
                async with session.begin():
                    subject = await add_subject(
                        session, user, int(data["class_id"]), value
                    )
            await _finish_flow(
                message, state, f"✅ درس «{html(subject.name)}» به کلاس اضافه شد.",
                class_id=int(data["class_id"]),
            )
            return

        if flow == "record":
            await _handle_record_input(message, state, bot, user, data, value)
            return

        if flow == "search":
            class_id = int(data["class_id"])
            async with SessionFactory() as session:
                async with session.begin():
                    homework = await list_homework(session, user, class_id)
                    exams = await list_exams(session, user, class_id)
            term = value.casefold()
            hw_matches = [
                (item, subject) for item, subject in homework
                if term in item.title.casefold() or term in subject.name.casefold()
            ]
            ex_matches = [
                (item, subject) for item, subject in exams
                if term in item.title.casefold() or term in subject.name.casefold()
            ]
            body = _format_homework(hw_matches) + "\n\n" + _format_exams(
                ex_matches, get_settings().default_timezone
            )
            await _finish_flow(message, state, body, class_id=class_id)
            return

        raise DomainError("فرم منقضی شده است. از /start دوباره آغاز کن.")
    except DomainError as exc:
        await _edit_saved_prompt(message, state, f"⚠️ {html(str(exc))}\nاطلاعات را اصلاح و دوباره ارسال کن.")
    except (ValueError, TypeError) as exc:
        await _edit_saved_prompt(message, state, f"⚠️ قالب ورودی درست نیست. {html(str(exc))}")


async def _edit_saved_prompt(message: Message, state: FSMContext, prompt: str) -> None:
    data = await state.get_data()
    panel_id = data.get("panel_message_id")
    chat_id = data.get("panel_chat_id", message.chat.id)
    if panel_id:
        try:
            await message.bot.edit_message_text(
                prompt,
                chat_id=chat_id,
                message_id=panel_id,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[button("❌ لغو فرم", "v2:form:cancel")]]
                ),
            )
            return
        except TelegramBadRequest:
            pass
    sent = await message.answer(
        prompt,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[button("❌ لغو فرم", "v2:form:cancel")]]
        ),
    )
    await state.update_data(panel_message_id=sent.message_id, panel_chat_id=sent.chat.id)


async def _handle_record_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    user: User,
    data: dict,
    value: str,
) -> None:
    record_type = RecordType(data["record_type"])
    class_id = int(data["class_id"])
    if record_type == RecordType.REPORT:
        async with SessionFactory() as session:
            async with session.begin():
                _, _, school = await require_class_access(session, user, class_id)
                today = datetime.now(ZoneInfo(school.timezone)).date()
                record = await create_daily_report(
                    session,
                    user,
                    class_id,
                    report_date=today,
                    title="گزارش روزانه",
                    body=value,
                )
        await _send_pending_notice(
            bot, class_id,
            f"🕓 گزارش روزانه جدید برای بررسی ثبت شد.\nتاریخ: {jalali_date(today)}\nشناسه: {record.id}",
        )
        await _finish_flow(
            message, state, "✅ گزارش ثبت شد و برای بررسی ارسال شد.", class_id=class_id
        )
        return

    if data["step"] == "title":
        await state.update_data(title=normalize_name(value, maximum=180), step="details")
        prompt = (
            "شرح تکلیف را وارد کن."
            if record_type == RecordType.HOMEWORK
            else "محدوده امتحان را وارد کن."
        )
        await _edit_saved_prompt(message, state, prompt)
        return

    if data["step"] == "details":
        await state.update_data(details=value, step="due" if record_type == RecordType.HOMEWORK else "datetime")
        prompt = (
            "موعد تحویل را به شکل ۱۴۰۵/۰۸/۲۷ وارد کن؛ برای بدون موعد «-» بفرست."
            if record_type == RecordType.HOMEWORK
            else "زمان امتحان را به شکل ۱۴۰۵/۰۸/۲۷ ۰۹:۰۰ وارد کن."
        )
        await _edit_saved_prompt(message, state, prompt)
        return

    async with SessionFactory() as session:
        async with session.begin():
            class_group, _, school = await require_class_access(session, user, class_id)
            if record_type == RecordType.HOMEWORK:
                due = None if value == "-" else parse_school_date(value)
                record = await create_homework(
                    session,
                    user,
                    class_id,
                    subject_id=int(data["subject_id"]),
                    title=data["title"],
                    description=data["details"],
                    due_on=due,
                )
                due_text = jalali_date(due) if due else "بدون موعد"
                message_text = f"🕓 تکلیف جدید برای بررسی\n{html(record.title)}\nموعد: {due_text}"
            else:
                scheduled = parse_school_datetime(value, school.timezone)
                record = await create_exam(
                    session,
                    user,
                    class_id,
                    subject_id=int(data["subject_id"]),
                    title=data["title"],
                    scope=data["details"],
                    scheduled_at=scheduled,
                )
                message_text = f"🕓 امتحان جدید برای بررسی\n{html(record.title)}"
    await _send_pending_notice(bot, class_id, message_text)
    await _finish_flow(
        message, state, "✅ اطلاعات ثبت شد و در انتظار تأیید مدیر کلاس است.",
        class_id=class_id,
    )


@router.callback_query(F.data == "v2:form:cancel")
async def cancel_flow_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    class_id = data.get("class_id")
    await state.clear()
    user = await _load_user_from_callback(callback)
    if class_id:
        await _show_class_dashboard(callback, state, user, int(class_id))
    else:
        await home_callback(callback, state)


@router.callback_query(F.data == "v2:noop")
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@router.errors()
async def handle_router_error(event, exception: Exception) -> bool:
    logger.exception("Unhandled ClassMate V2 update error", exc_info=exception)
    return True
