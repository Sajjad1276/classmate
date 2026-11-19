import logging
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BotCommandScopeChat,
    CallbackQuery,
    Message,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.bot.keyboards import (
    approval_keyboard,
    class_picker,
    main_menu,
    main_reply_keyboard,
    group_menu_keyboard,
    wizard_cancel_keyboard,
    new_record_menu,
    reminder_class_picker,
    reminder_preferences_keyboard,
    subject_picker,
)
from classmate.bot.states import RecordWizard
from classmate.core.config import get_settings
from classmate.db.models import (
    AcademicYear,
    ClassGroup,
    ClassMembership,
    ClassSubject,
    School,
    SchoolMembership,
    NotificationPreference,
    Subject,
    TimetableEntry,
    User,
)
from classmate.db.session import SessionFactory
from classmate.domain.dates import (
    jalali_date,
    jalali_datetime,
    parse_school_date,
    parse_school_datetime,
)
from classmate.services.notifications import update_user_reminder_preference
from classmate.services.permissions import (
    can_approve,
    ensure_user,
    get_class_with_school,
    hash_code,
    is_school_admin,
    make_code,
    user_can_access_class,
)
from classmate.services.records import (
    add_subject,
    approve_record,
    audit,
    create_daily_report,
    create_exam,
    create_homework,
    get_group_context,
    get_subject,
    get_or_create_next_lesson,
    list_exams,
    list_homework,
    list_homework_for_lesson,
    list_pending_records,
    normalize_subject_name,
    reject_record,
    list_daily_summary,
    list_current_timetable,
    get_record_submitter_telegram_id,
)

router = Router(name="classmate")
logger = logging.getLogger(__name__)
WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]


async def _user(session: AsyncSession, tg_user, private: bool = False) -> User:
    settings = get_settings()
    user = await ensure_user(session, tg_user, private_chat_started=private)
    if tg_user.id in settings.bootstrap_admin_id_set:
        user.is_platform_admin = True
    return user


async def _main_keyboard_for_user(session: AsyncSession, user: User):
    platform_admin = user.is_platform_admin
    school_admin = platform_admin or (
        await session.scalar(
            select(SchoolMembership.id).where(
                SchoolMembership.user_id == user.id,
                SchoolMembership.role == "school_admin",
                SchoolMembership.is_active.is_(True),
            )
        )
    ) is not None
    class_roles = set(
        (
            await session.scalars(
                select(ClassMembership.role).where(
                    ClassMembership.user_id == user.id,
                    ClassMembership.is_active.is_(True),
                )
            )
        ).all()
    )
    class_manager = "class_manager" in class_roles
    approver = school_admin or class_manager or "trusted_approver" in class_roles
    return main_reply_keyboard(
        is_platform_admin=platform_admin,
        is_school_admin=school_admin,
        is_class_manager=class_manager,
        is_approver=approver,
    )


async def _classes_for_user(session: AsyncSession, user: User) -> list[ClassGroup]:
    query = (
        select(ClassGroup)
        .join(AcademicYear, ClassGroup.academic_year_id == AcademicYear.id)
        .outerjoin(
            ClassMembership,
            (ClassMembership.class_id == ClassGroup.id)
            & (ClassMembership.user_id == user.id)
            & ClassMembership.is_active.is_(True),
        )
        .outerjoin(
            SchoolMembership,
            (SchoolMembership.school_id == AcademicYear.school_id)
            & (SchoolMembership.user_id == user.id)
            & (SchoolMembership.role == "school_admin")
            & SchoolMembership.is_active.is_(True),
        )
        .where(
            ClassGroup.is_active.is_(True),
            or_(
                ClassMembership.id.is_not(None),
                SchoolMembership.id.is_not(None),
                user.is_platform_admin,
            ),
        )
        .distinct()
        .order_by(ClassGroup.name)
    )
    return list((await session.execute(query)).scalars().all())


async def _class_from_message(
    session: AsyncSession, message: Message, class_id: int | None = None,
    user: User | None = None,
):
    if message.chat.type in {"group", "supergroup"}:
        return await get_group_context(session, message.chat.id)
    if class_id is None or user is None:
        return None
    context = await get_class_with_school(session, class_id)
    if context is None or not await user_can_access_class(session, user, class_id):
        return None
    return context


async def _ensure_group_member(
    session: AsyncSession, user: User, class_id: int
) -> None:
    membership = await session.scalar(
        select(ClassMembership).where(
            ClassMembership.class_id == class_id, ClassMembership.user_id == user.id
        )
    )
    if membership is None:
        session.add(ClassMembership(class_id=class_id, user_id=user.id, role="student"))
    elif not membership.is_active:
        membership.is_active = True


async def _subject_list(session: AsyncSession, class_id: int) -> list[Subject]:
    return list(
        (
            await session.execute(
                select(Subject)
                .join(ClassSubject, ClassSubject.subject_id == Subject.id)
                .where(ClassSubject.class_id == class_id)
                .order_by(Subject.name)
            )
        ).scalars().all()
    )


async def _pending_message(
    bot: Bot, class_group: ClassGroup, entity_type: str, record_id: int,
    title: str, details: str, creator_name: str,
) -> None:
    body = (
        f"🕓 <b>درخواست جدید برای بررسی</b>\n"
        f"نوع: {escape(entity_type)}\n"
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


TELEGRAM_SAFE_TEXT_LIMIT = 3600


def _append_bounded(parts: list[str], block: str) -> bool:
    """Append one complete HTML block only when the encoded message stays bounded."""
    candidate = "\n\n".join([*parts, block])
    # Telegram counts UTF-16 code units; escaped HTML entities are included here.
    if len(candidate.encode("utf-16-le")) // 2 > TELEGRAM_SAFE_TEXT_LIMIT:
        return False
    parts.append(block)
    return True


def _homework_block(homework, subject, description_limit: int = 160) -> str:
    due = jalali_date(homework.due_on) if homework.due_on else "نامشخص"
    description = (homework.description or "")[:description_limit]
    if homework.description and len(homework.description) > description_limit:
        description += "…"
    return (
        f"• <b>{escape(subject.name[:40])}</b> — {escape(homework.title[:60])}\n"
        f"  {escape(description)}\n  موعد: {due}"
    )


def _exam_block(exam, subject, timezone_name: str, scope_limit: int = 100) -> str:
    scope = (exam.scope or "اعلام نشده")[:scope_limit]
    if exam.scope and len(exam.scope) > scope_limit:
        scope += "…"
    return (
        f"• <b>{escape(subject.name[:40])}</b> — {escape(exam.title[:60])}\n"
        f"  محدوده: {escape(scope)}\n"
        f"  زمان: {jalali_datetime(exam.scheduled_at, timezone_name)}"
    )


def _format_homework(rows) -> str:
    if not rows:
        return "هنوز تکلیف تأییدشده‌ای ثبت نشده است."
    parts = ["📚 <b>تکالیف تأییدشده</b>"]
    omitted = max(0, len(rows) - 8)
    for homework, subject in rows[:8]:
        if not _append_bounded(parts, _homework_block(homework, subject)):
            omitted += 1
    if omitted:
        parts.append(f"ℹ️ {omitted} تکلیف دیگر به‌دلیل محدودیت طول پیام نمایش داده نشد.")
    return "\n\n".join(parts)


def _format_exams(rows, timezone_name: str) -> str:
    if not rows:
        return "امتحان آینده‌ای ثبت نشده است."
    parts = ["📝 <b>امتحانات تأییدشده</b>"]
    omitted = max(0, len(rows) - 8)
    for exam, subject in rows[:8]:
        if not _append_bounded(parts, _exam_block(exam, subject, timezone_name)):
            omitted += 1
    if omitted:
        parts.append(f"ℹ️ {omitted} امتحان دیگر به‌دلیل محدودیت طول پیام نمایش داده نشد.")
    return "\n\n".join(parts)


def _format_daily_summary(reports, homework_rows, exam_rows, local_date, timezone_name: str) -> str:
    parts = [f"🗓 <b>خلاصه امروز — {jalali_date(local_date)}</b>"]
    omitted = max(0, len(reports) - 3) + max(0, len(homework_rows) - 4) + max(0, len(exam_rows) - 4)

    if reports:
        for report in reports[:3]:
            title = escape((report.title or "گزارش روزانه")[:60])
            body = (report.body or "")[:140]
            if report.body and len(report.body) > 140:
                body += "…"
            block = f"🗒 <b>{title}</b>\n{escape(body)}"
            if not _append_bounded(parts, block):
                omitted += 1
    else:
        _append_bounded(parts, "🗒 گزارش تأییدشده‌ای برای امروز ثبت نشده است.")

    if homework_rows:
        _append_bounded(parts, "📚 <b>تکالیف موعددار امروز</b>")
        for homework, subject in homework_rows[:4]:
            if not _append_bounded(parts, _homework_block(homework, subject, 90)):
                omitted += 1
    else:
        _append_bounded(parts, "📚 امروز تکلیف موعدداری ثبت نشده است.")

    if exam_rows:
        _append_bounded(parts, "📝 <b>امتحانات امروز</b>")
        for exam, subject in exam_rows[:4]:
            if not _append_bounded(parts, _exam_block(exam, subject, timezone_name, 70)):
                omitted += 1
    else:
        _append_bounded(parts, "📝 امروز امتحانی ثبت نشده است.")

    if omitted:
        parts.append("ℹ️ موارد بیشتری وجود دارد که به‌دلیل محدودیت طول پیام در این خلاصه نمایش داده نشد.")
    return "\n\n".join(parts)


def _parse_pipe_args(raw: str, count: int) -> list[str]:
    values = [part.strip() for part in raw.split("|")]
    if len(values) != count or any(not value for value in values):
        raise ValueError(f"تعداد یا قالب ورودی اشتباه است؛ {count} بخش را با | جدا کنید.")
    return values


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.regexp(r"^/start(?:@\w+)?\s+CM_[A-Z0-9_-]{1,32}$"),
)
async def connect_school_group(message: Message, bot: Bot):
    """Consume a one-time startgroup payload and bind this Telegram group to a school."""
    if message.from_user is None or message.text is None:
        return
    payload = message.text.split(maxsplit=1)[1].strip()
    try:
        bot_profile = await bot.get_me()
        bot_member = await bot.get_chat_member(message.chat.id, bot_profile.id)
        if bot_member.status not in {"administrator", "creator"}:
            await message.answer(
                "برای اتصال، ابتدا ربات را در این گروه مدیر کن و سپس دوباره تلاش کن."
            )
            return
        group_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    except Exception:
        await message.answer(
            "امکان بررسی دسترسی مدیر گروه وجود ندارد. مطمئن شو ربات مدیر گروه است."
        )
        return
    if group_member.status not in {"administrator", "creator"}:
        await message.answer("فقط مدیر گروه تلگرام می‌تواند اتصال مدرسه را انجام دهد.")
        return

    async with SessionFactory() as session:
        async with session.begin():
            actor = await _user(session, message.from_user, private=False)
            await session.execute(select(func.pg_advisory_xact_lock(message.chat.id)))
            now = datetime.now(timezone.utc)
            school = await session.scalar(
                select(School)
                .where(
                    School.group_link_token_hash == hash_code(payload),
                    School.group_link_token_expires_at > now,
                    School.is_active.is_(True),
                )
                .with_for_update()
            )
            if school is None:
                await message.answer(
                    "این لینک اتصال معتبر نیست، منقضی شده یا قبلاً استفاده شده است. "
                    "لینک تازه را از پنل مدرسه دریافت کن."
                )
                return
            if not actor.is_platform_admin and not await is_school_admin(session, actor, school.id):
                await message.answer("فقط مدیر همین مدرسه یا مدیر کل می‌تواند گروه مدرسه را متصل کند.")
                return

            class_binding = await session.scalar(
                select(ClassGroup.id).where(ClassGroup.group_chat_id == message.chat.id)
            )
            if class_binding is not None:
                await message.answer("این گروه قبلاً به یک کلاس متصل شده است؛ برای مدرسه یک گروه جدا بساز.")
                return
            already_used = await session.scalar(
                select(School.id).where(
                    School.telegram_group_id == message.chat.id,
                    School.id != school.id,
                )
            )
            if already_used is not None:
                await message.answer("این گروه قبلاً به مدرسه دیگری متصل شده است.")
                return

            if school.telegram_group_id not in {None, message.chat.id}:
                await message.answer("برای این مدرسه قبلاً گروه دیگری متصل شده است.")
                return

            school.telegram_group_id = message.chat.id
            school.telegram_group_title = (message.chat.title or "گروه مدرسه")[:180]
            school.group_link_token_hash = None
            school.group_link_token_expires_at = None
            school_name = school.name

    confirmation = (
        f"✅ گروه «{escape(message.chat.title or 'گروه مدرسه')}» به مدرسه "
        f"«{escape(school_name)}» متصل شد.\n"
        "از این پس این گروه، گروه اصلی اطلاع‌رسانی آن مدرسه خواهد بود."
    )
    await message.answer(confirmation)
    try:
        await bot.send_message(message.from_user.id, confirmation)
    except Exception:
        logger.info("Could not send private school-group confirmation to user %s", message.from_user.id)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot):
    if message.chat.type == "private":
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=message.chat.id))
    previous = await state.get_data()
    panel_id = previous.get("panel_message_id")
    if panel_id and message.chat.type == "private":
        try:
            await bot.delete_message(message.chat.id, panel_id)
        except Exception:
            pass
    await state.clear()
    if message.chat.type == "private":
        await state.update_data(reply_keyboard_visible=True)
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            keyboard = (
                await _main_keyboard_for_user(session, user)
                if message.chat.type == "private"
                else None
            )
            group_context = (
                await get_group_context(session, message.chat.id)
                if message.chat.type in {"group", "supergroup"}
                else None
            )
            group_can_review = bool(
                group_context and await can_approve(session, user, group_context[0].id)
            )
            school_group_name = None
            if message.chat.type in {"group", "supergroup"} and group_context is None:
                school_group_name = await session.scalar(
                    select(School.name).where(
                        School.telegram_group_id == message.chat.id,
                        School.is_active.is_(True),
                    )
                )
            platform_admin = user.is_platform_admin

    if group_context:
        class_group, _, school = group_context
        await message.answer(
            f"🏫 <b>{escape(class_group.name)}</b> — {escape(school.name)}\n"
            "منوی این کلاس:",
            reply_markup=group_menu_keyboard(can_review=group_can_review),
        )
        return
    if school_group_name:
        await message.answer(
            f"🏫 <b>گروه مرکزی مدرسه {escape(school_group_name)}</b>\n"
            "این گروه برای اطلاع‌رسانی مدرسه متصل است. برای ثبت و مشاهده اطلاعات درسی، "
            "از گروه اختصاصی هر کلاس استفاده کن."
        )
        return

    greeting = f"سلام {escape(message.from_user.first_name or '')} 👋\n"
    greeting += (
        "به ClassMate خوش آمدی. تمام بخش‌ها را با دکمه‌های پایین صفحه باز کن.\n"
        "اطلاعات ثبت‌شده تا پیش از تأیید افراد مورد اعتماد، در فهرست قطعی کلاس نمایش داده نمی‌شوند."
    )
    if platform_admin:
        greeting += "\n\n🛡 پنل مدیر کل برای این حساب فعال است."
    await message.answer(greeting, reply_markup=keyboard)


@router.message(Command("help"))
async def help_command(message: Message):
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            keyboard = (
                await _main_keyboard_for_user(session, user)
                if message.chat.type == "private"
                else None
            )
    await message.answer(
        "<b>راهنمای ClassMate</b>\n"
        "برای استفاده از ربات، از دکمه‌های پایین صفحه استفاده کن.\n"
        "گزینه‌های مدیریتی و بررسی فقط برای نقش‌های مجاز نمایش داده می‌شوند.\n"
        "برای بازگشت از هر پنل، دکمه «بازگشت» همان پنل را بزن.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("group:"))
async def group_menu_action(callback: CallbackQuery, bot: Bot):
    if callback.message.chat.type not in {"group", "supergroup"}:
        await callback.answer("این منو فقط در گروه کلاس فعال است.", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    pending = []
    class_name = ""
    answer = ""
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=False)
            context = await get_group_context(session, callback.message.chat.id)
            if not context:
                await callback.answer("این گروه به یک کلاس فعال متصل نیست.", show_alert=True)
                return
            class_group, _, school = context
            class_name = class_group.name

            if action == "pending":
                if not await can_approve(session, user, class_group.id):
                    await callback.answer("مجوز مشاهده صف بررسی را نداری.", show_alert=True)
                    return
                pending = await list_pending_records(session, class_group.id)
            elif action == "homework":
                answer = _format_homework(await list_homework(session, class_group.id))
            elif action == "exams":
                answer = _format_exams(
                    await list_exams(session, class_group.id), school.timezone
                )
            elif action == "week":
                local_date = datetime.now(ZoneInfo(school.timezone)).date()
                entries = await list_current_timetable(
                    session, class_group.id, local_date
                )
                if not entries:
                    answer = "برنامه هفتگی هنوز برای این کلاس ثبت نشده است."
                else:
                    rows = [
                        f"{WEEKDAYS[entry.weekday]} — زنگ {entry.period}"
                        + (f" ({entry.starts_at.strftime('%H:%M')})" if entry.starts_at else "")
                        + f": {escape(subject.name)}"
                        for entry, subject in entries
                    ]
                    answer = "📅 <b>برنامه هفتگی</b>\n" + "\n".join(rows)
            elif action == "today":
                zone = ZoneInfo(school.timezone)
                local_date = datetime.now(zone).date()
                reports, homework_rows, exam_rows = await list_daily_summary(
                    session, class_group.id, local_date, school.timezone
                )
                answer = _format_daily_summary(
                    reports, homework_rows, exam_rows, local_date, school.timezone
                )
            else:
                await callback.answer("این گزینه شناخته‌شده نیست.", show_alert=True)
                return

    if action == "pending":
        if not pending:
            await callback.message.answer("🕓 موردی در انتظار بررسی نیست.")
        else:
            for kind, record in pending[:8]:
                title = getattr(record, "title", "گزارش روزانه")
                details = getattr(
                    record, "description",
                    getattr(record, "body", getattr(record, "scope", "")),
                )
                await callback.message.answer(
                    f"🕓 <b>{escape(kind)} #{record.id} — {escape(title)}</b>\n"
                    f"{escape(details[:1200])}",
                    reply_markup=approval_keyboard(kind, record.id),
                )
            if len(pending) > 8:
                await callback.message.answer("فقط ۸ مورد نخست صف بررسی نمایش داده شد.")
    else:
        await callback.message.answer(
            f"🏫 <b>{escape(class_name)}</b>\n\n{answer}"
        )
    await callback.answer()


@router.callback_query(F.data == "menu:home")
async def menu_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("منوی اصلی ClassMate:", reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:new")
async def menu_new(callback: CallbackQuery):
    await callback.message.answer("چه اطلاعاتی می‌خواهی ثبت کنی؟", reply_markup=new_record_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:classes")
async def menu_classes(callback: CallbackQuery):
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            classes = await _classes_for_user(session, user)
            rows = []
            for item in classes:
                ctx = await get_class_with_school(session, item.id)
                rows.append((item.name, ctx[2].name if ctx else ""))
    if not rows:
        text = "هنوز به کلاسی متصل نیستی. برای عضویت، کد دعوت کلاس را با /join وارد کن."
    else:
        text = "🏫 <b>کلاس‌های تو</b>\n" + "\n".join(
            f"• {escape(name)} — {escape(school)}" for name, school in rows
        )
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.in_({"menu:homework", "menu:exams", "menu:week", "menu:search"}))
async def menu_shortcuts(callback: CallbackQuery):
    messages = {
        "menu:homework": "برای مشاهده تکالیف در خصوصی از /homework شناسه_کلاس و در گروه از /homework استفاده کن.",
        "menu:exams": "برای مشاهده امتحانات از /exams شناسه_کلاس یا فرمان /exams در گروه استفاده کن.",
        "menu:week": "برای مشاهده برنامه هفتگی از /week شناسه_کلاس یا /week در گروه استفاده کن.",
        "menu:search": "جست‌وجوی نسخه اول مبتنی بر کلیدواژه است. از /ask نام درس یا عبارت موردنظر استفاده کن.",
    }
    await callback.message.answer(messages[callback.data])
    await callback.answer()


async def _show_reminder_class_selector(callback: CallbackQuery) -> None:
    if callback.message.chat.type != "private":
        await callback.answer("تنظیم یادآوری خصوصی فقط در گفت‌وگوی خصوصی در دسترس است.", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            classes = await _classes_for_user(session, user)
    text = (
        "برای تنظیم یادآوری ابتدا عضو یک کلاس شو."
        if not classes
        else "🔔 <b>تنظیم یادآوری خصوصی</b>\nکلاس موردنظر را انتخاب کن:"
    )
    markup = reminder_class_picker(classes) if classes else None
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


async def _show_reminder_preferences(callback: CallbackQuery, class_id: int) -> None:
    if callback.message.chat.type != "private":
        await callback.answer("این تنظیم فقط در گفت‌وگوی خصوصی قابل تغییر است.", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            if not await user_can_access_class(session, user, class_id):
                await callback.answer("به این کلاس دسترسی نداری.", show_alert=True)
                return
            context = await get_class_with_school(session, class_id)
            preference = await session.scalar(
                select(NotificationPreference).where(
                    NotificationPreference.user_id == user.id,
                    NotificationPreference.class_id == class_id,
                )
            )
            homework_enabled = preference.homework_enabled if preference else True
            exams_enabled = preference.exams_enabled if preference else True
    class_name = context[0].name if context else "کلاس"
    await callback.message.edit_text(
        f"🔔 <b>تنظیم یادآوری — {escape(class_name)}</b>\n"
        "این تنظیم فقط روی پیام‌های خصوصی اثر دارد؛ اعلان گروهی کلاس جداگانه ارسال می‌شود.",
        reply_markup=reminder_preferences_keyboard(
            class_id,
            homework_enabled=homework_enabled,
            exams_enabled=exams_enabled,
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:reminders")
async def menu_reminders(callback: CallbackQuery):
    await _show_reminder_class_selector(callback)


@router.callback_query(F.data == "reminders:list")
async def reminder_class_list(callback: CallbackQuery):
    await _show_reminder_class_selector(callback)


@router.callback_query(F.data.startswith("reminders:class:"))
async def reminder_class_selected(callback: CallbackQuery):
    try:
        class_id = int(callback.data.rsplit(":", 1)[1])
        if class_id < 1:
            raise ValueError
    except (TypeError, ValueError):
        await callback.answer("شناسه کلاس نامعتبر است.", show_alert=True)
        return
    await _show_reminder_preferences(callback, class_id)


@router.callback_query(F.data.startswith("reminders:set:"))
async def reminder_preference_set(callback: CallbackQuery):
    try:
        _, _, category, class_id_raw, enabled_raw = callback.data.split(":")
        class_id = int(class_id_raw)
        if category not in {"homework", "exams"} or class_id < 1 or enabled_raw not in {"0", "1"}:
            raise ValueError
        enabled = enabled_raw == "1"
    except (TypeError, ValueError):
        await callback.answer("درخواست تنظیم یادآوری نامعتبر است.", show_alert=True)
        return
    if callback.message.chat.type != "private":
        await callback.answer("تنظیم یادآوری فقط در خصوصی قابل تغییر است.", show_alert=True)
        return

    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            if not await user_can_access_class(session, user, class_id):
                await callback.answer("به این کلاس دسترسی نداری.", show_alert=True)
                return
            context = await get_class_with_school(session, class_id)
            if not context:
                await callback.answer("کلاس فعال پیدا نشد.", show_alert=True)
                return
            class_group, _, school = context
            await update_user_reminder_preference(
                session, user, class_group, school.timezone, category, enabled
            )

    await _show_reminder_preferences(callback, class_id)


@router.message(Command("reminders_off", "reminders_on"))
async def reminder_preferences(message: Message):
    if message.chat.type != "private":
        await message.answer("تنظیم یادآوری خصوصی را در گفت‌وگوی خصوصی ربات انجام بده.")
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "این فرمان برای سازگاری قبلی است. از دکمه «🔔 یادآوری» استفاده کن تا کلاس را از فهرست انتخاب کنی.\n"
            "قالب قدیمی: /reminders_on شناسه_کلاس [homework|exams|all]"
        )
        return
    class_id = int(parts[1])
    category = parts[2].lower() if len(parts) > 2 else "all"
    if category not in {"homework", "exams", "all"}:
        await message.answer("نوع یادآوری باید homework، exams یا all باشد.")
        return
    enabled = parts[0].startswith("/reminders_on")
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=True)
            context = await _class_from_message(session, message, class_id, user)
            if not context:
                await message.answer("به این کلاس دسترسی نداری.")
                return
            class_group, _, school = context
            if category in {"homework", "all"}:
                await update_user_reminder_preference(
                    session, user, class_group, school.timezone, "homework", enabled
                )
            if category in {"exams", "all"}:
                await update_user_reminder_preference(
                    session, user, class_group, school.timezone, "exams", enabled
                )
    state_label = "فعال" if enabled else "غیرفعال"
    await message.answer(
        f"✅ یادآوری‌های انتخاب‌شده برای «{escape(class_group.name)}» {state_label} شدند."
    )
