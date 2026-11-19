import logging
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommandScopeChat, CallbackQuery, Message
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.bot.formatters import _format_exams, _format_homework, _pending_message
from classmate.bot.keyboards import (
    approval_keyboard,
    class_picker,
    main_menu,
    main_reply_keyboard,
    wizard_cancel_keyboard,
    new_record_menu,
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
    ReminderJob,
    Subject,
    TimetableEntry,
    User,
)
from classmate.db.session import SessionFactory
from classmate.domain.enums import ENTITY_LABELS, EntityType, RecordStatus, Role
from classmate.domain.dates import (
    jalali_date,
    jalali_datetime,
    parse_school_date,
    parse_school_datetime,
)
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
    cancel_approved_record,
    get_group_context,
    get_subject,
    get_or_create_next_lesson,
    list_exams,
    list_homework,
    list_homework_for_lesson,
    list_pending_records,
    list_reports,
    normalize_subject_name,
    reject_record,
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
                SchoolMembership.role == Role.SCHOOL_ADMIN,
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
    class_manager = Role.CLASS_MANAGER in class_roles
    approver = school_admin or class_manager or Role.TRUSTED_APPROVER in class_roles
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
            & (SchoolMembership.role == Role.SCHOOL_ADMIN)
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
        session.add(ClassMembership(class_id=class_id, user_id=user.id, role=Role.STUDENT))
    elif not membership.is_active:
        membership.is_active = True


async def _subject_list(session: AsyncSession, class_id: int) -> list[Subject]:
    return list(
        (
            await session.execute(
                select(Subject)
                .join(ClassSubject, ClassSubject.subject_id == Subject.id)
                .where(
                    ClassSubject.class_id == class_id,
                    ClassSubject.deleted_at.is_(None),
                    Subject.deleted_at.is_(None),
                )
                .order_by(Subject.name)
            )
        ).scalars().all()
    )




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

    async with SessionFactory() as session:
        async with session.begin():
            actor = await _user(session, message.from_user, private=False)
            now = datetime.now(timezone.utc)
            school = await session.scalar(
                select(School).where(
                    School.group_link_token_hash == hash_code(payload),
                    School.group_link_token_expires_at > now,
                    School.is_active.is_(True),
                )
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
        f"«{escape(school_name)}» متصل شد.\\n"
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
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            keyboard = (
                await _main_keyboard_for_user(session, user)
                if message.chat.type == "private"
                else None
            )
    greeting = (
        f"• درود بیکران 🕊 {escape(message.from_user.full_name)}، به ربات ClassMate خوش آمدید.\n\n"
        "📇 - در اینجا می‌تونید امور مدرسه رو به صورت حرفه‌ای و پیشرفته مدیریت کنید.\n\n"
        f"• آیدی شما : <code>{message.from_user.id}</code>\n\n"
        "🪎جهت ادامه دستور مورد نظر رو از پنل زیر وارد کنید:"
    )
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


@router.callback_query(F.data.in_({"menu:homework", "menu:exams", "menu:week", "menu:search", "menu:reminders"}))
async def menu_shortcuts(callback: CallbackQuery):
    messages = {
        "menu:homework": "برای مشاهده تکالیف در خصوصی از /homework شناسه_کلاس و در گروه از /homework استفاده کن.",
        "menu:exams": "برای مشاهده امتحانات از /exams شناسه_کلاس یا فرمان /exams در گروه استفاده کن.",
        "menu:week": "برای مشاهده برنامه هفتگی از /week شناسه_کلاس یا /week در گروه استفاده کن.",
        "menu:search": "جست‌وجوی نسخه اول مبتنی بر کلیدواژه است. از /ask نام درس یا عبارت موردنظر استفاده کن.",
        "menu:reminders": "یادآوری‌های خصوصی فقط برای کاربرانی ارسال می‌شود که ربات را در خصوصی Start کرده باشند. تنظیمات پیش‌فرض کلاس فعال است.",
    }
    await callback.message.answer(messages[callback.data])
    await callback.answer()


@router.message(Command("school_new"))
async def school_new(message: Message):
    try:
        values = [part.strip() for part in message.text.split(maxsplit=1)[1].split("|")]
        if len(values) not in {2, 3} or not values[0] or not values[1]:
            raise ValueError
    except (IndexError, ValueError):
        await message.answer("قالب: /school_new نام مدرسه | ۱۴۰۵-۱۴۰۶ [| Asia/Tehran]")
        return
    settings = get_settings()
    if message.chat.type != "private":
        await message.answer("این فرمان را در گفت‌وگوی خصوصی ربات اجرا کن.")
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=True)
            if not user.is_platform_admin:
                await message.answer("فقط مدیر کل سامانه می‌تواند مدرسه ایجاد کند.")
                return
            timezone_name = values[2] if len(values) == 3 else settings.default_timezone
            try:
                ZoneInfo(timezone_name)
            except Exception:
                await message.answer("منطقه زمانی معتبر نیست؛ نمونه: Asia/Tehran")
                return
            school = School(name=values[0][:180], timezone=timezone_name)
            session.add(school)
            await session.flush()
            year = AcademicYear(school_id=school.id, name=values[1][:40], is_current=True)
            session.add(year)
            session.add(
                SchoolMembership(school_id=school.id, user_id=user.id, role=Role.SCHOOL_ADMIN)
            )
            await session.flush()
            school_id = school.id
    await message.answer(
        f"✅ مدرسه ایجاد شد.\nشناسه مدرسه: <code>{school_id}</code>\n"
        f"سال تحصیلی: {escape(values[1])}\nمنطقه زمانی: {escape(timezone_name)}"
    )



@router.message(Command("school_admin_add"))
async def school_admin_add(message: Message):
    if message.chat.type != "private":
        await message.answer("این فرمان را در گفت‌وگوی خصوصی ربات اجرا کن.")
        return
    try:
        school_text, telegram_text = _parse_pipe_args(message.text.split(maxsplit=1)[1], 2)
        school_id, telegram_id = int(school_text), int(telegram_text)
    except (IndexError, ValueError):
        await message.answer("قالب: /school_admin_add شناسه_مدرسه | شناسه_تلگرام")
        return
    async with SessionFactory() as session:
        async with session.begin():
            actor = await _user(session, message.from_user, private=True)
            if not actor.is_platform_admin:
                await message.answer("فقط مدیر کل سامانه می‌تواند مدیر مدرسه تعیین کند.")
                return
            school = await session.get(School, school_id)
            target = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if school is None or target is None:
                await message.answer("مدرسه یا کاربر پیدا نشد. کاربر باید ابتدا ربات را Start کند.")
                return
            membership = await session.scalar(
                select(SchoolMembership).where(
                    SchoolMembership.school_id == school_id,
                    SchoolMembership.user_id == target.id,
                )
            )
            if membership is None:
                session.add(
                    SchoolMembership(school_id=school_id, user_id=target.id, role=Role.SCHOOL_ADMIN)
                )
            else:
                membership.role = Role.SCHOOL_ADMIN
                membership.is_active = True
    await message.answer("✅ مدیر مدرسه تعیین شد.")


@router.message(Command("year_new"))
async def year_new(message: Message):
    if message.chat.type != "private":
        await message.answer("این فرمان را در گفت‌وگوی خصوصی ربات اجرا کن.")
        return
    try:
        school_text, year_name = _parse_pipe_args(message.text.split(maxsplit=1)[1], 2)
        school_id = int(school_text)
    except (IndexError, ValueError):
        await message.answer("قالب: /year_new شناسه_مدرسه | ۱۴۰۶-۱۴۰۷")
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=True)
            if not await is_school_admin(session, user, school_id):
                await message.answer("مجوز مدیریت این مدرسه را نداری.")
                return
            existing = await session.scalar(
                select(AcademicYear.id).where(
                    AcademicYear.school_id == school_id,
                    AcademicYear.name == year_name,
                )
            )
            if existing is not None:
                await message.answer("این سال تحصیلی قبلاً ثبت شده است.")
                return
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
            year_id = year.id
    await message.answer(f"✅ سال تحصیلی جدید فعال شد. شناسه: <code>{year_id}</code>")


@router.message(Command("class_manager_add"))
async def class_manager_add(message: Message):
    if message.chat.type != "private":
        await message.answer("این فرمان را در گفت‌وگوی خصوصی ربات اجرا کن.")
        return
    try:
        class_text, telegram_text = _parse_pipe_args(message.text.split(maxsplit=1)[1], 2)
        class_id, telegram_id = int(class_text), int(telegram_text)
    except (IndexError, ValueError):
        await message.answer("قالب: /class_manager_add شناسه_کلاس | شناسه_تلگرام")
        return
    async with SessionFactory() as session:
        async with session.begin():
            actor = await _user(session, message.from_user, private=True)
            context = await get_class_with_school(session, class_id)
            target = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if context is None or target is None:
                await message.answer("کلاس یا کاربر پیدا نشد.")
                return
            if not await is_school_admin(session, actor, context[2].id):
                await message.answer("فقط مدیر مدرسه می‌تواند مدیر کلاس تعیین کند.")
                return
            membership = await session.scalar(
                select(ClassMembership).where(
                    ClassMembership.class_id == class_id,
                    ClassMembership.user_id == target.id,
                )
            )
            if membership is None:
                session.add(ClassMembership(class_id=class_id, user_id=target.id, role=Role.CLASS_MANAGER))
            else:
                membership.role = Role.CLASS_MANAGER
                membership.is_active = True
    await message.answer("✅ مدیر کلاس تعیین شد.")


@router.message(Command("class_new"))
async def class_new(message: Message):
    try:
        school_id, grade, section, name = _parse_pipe_args(
            message.text.split(maxsplit=1)[1], 4
        )
        school_id_int = int(school_id)
    except (IndexError, ValueError):
        await message.answer("قالب: /class_new شناسه_مدرسه | پایه | شعبه | نام کلاس")
        return
    if message.chat.type != "private":
        await message.answer("این فرمان را در گفت‌وگوی خصوصی ربات اجرا کن.")
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=True)
            school = await session.get(School, school_id_int)
            if school is None or not await is_school_admin(session, user, school_id_int):
                await message.answer("مدرسه پیدا نشد یا مجوز مدیریت آن را نداری.")
                return
            year = await session.scalar(
                select(AcademicYear).where(
                    AcademicYear.school_id == school_id_int,
                    AcademicYear.is_current.is_(True),
                )
            )
            if year is None:
                await message.answer("برای این مدرسه سال تحصیلی جاری تعریف نشده است.")
                return
            item = ClassGroup(
                academic_year_id=year.id,
                name=name[:80],
                grade=grade[:32],
                section=None if section == "-" else section[:32],
            )
            session.add(item)
            await session.flush()
            class_id = item.id
            session.add(ClassMembership(class_id=class_id, user_id=user.id, role=Role.CLASS_MANAGER))
    await message.answer(
        f"✅ کلاس ایجاد شد. شناسه کلاس: <code>{class_id}</code>\n"
        "برای تولید کد اتصال از /class_code استفاده کن."
    )


@router.message(Command("class_code"))
async def class_code(message: Message):
    try:
        class_id = int(message.text.split(maxsplit=1)[1].strip())
    except (IndexError, ValueError):
        await message.answer("قالب: /class_code شناسه_کلاس")
        return
    if message.chat.type != "private":
        await message.answer("این فرمان را در گفت‌وگوی خصوصی ربات اجرا کن.")
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=True)
            context = await get_class_with_school(session, class_id)
            if not context:
                await message.answer("کلاس پیدا نشد.")
                return
            class_group, _, school = context
            if not await is_school_admin(session, user, school.id):
                member = await session.scalar(
                    select(ClassMembership).where(
                        ClassMembership.class_id == class_id,
                        ClassMembership.user_id == user.id,
                        ClassMembership.role == Role.CLASS_MANAGER,
                        ClassMembership.is_active.is_(True),
                    )
                )
                if member is None:
                    await message.answer("برای تولید کد اتصال به این کلاس مجوز نداری.")
                    return
            connect_code, join_code = make_code(), make_code()
            now = datetime.now(timezone.utc)
            class_group.group_link_code_hash = hash_code(connect_code)
            class_group.group_link_code_expires_at = now + timedelta(minutes=30)
            class_group.member_join_code_hash = hash_code(join_code)
            class_group.member_join_code_expires_at = now + timedelta(days=7)
    await message.answer(
        f"🔐 <b>کدهای کلاس {escape(class_group.name)}</b>\n"
        f"کد اتصال گروه (یک‌بارمصرف، ۳۰ دقیقه): <code>{connect_code}</code>\n"
        f"کد عضویت دانش‌آموزان (۷ روز): <code>{join_code}</code>\n\n"
        "این کدها را فقط با افراد موردنظر به اشتراک بگذار. کد اتصال پس از استفاده باطل می‌شود."
    )


@router.message(Command("connect"))
async def connect_group(message: Message, bot: Bot):
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("این فرمان را داخل گروه موردنظر اجرا کن.")
        return
    try:
        code = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        await message.answer("قالب: /connect کد_اتصال")
        return
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in {"administrator", "creator"}:
            await message.answer("فقط مدیر گروه می‌تواند اتصال را انجام دهد.")
            return
    except Exception:
        await message.answer("امکان بررسی دسترسی مدیر گروه وجود ندارد. دوباره تلاش کن.")
        return
    async with SessionFactory() as session:
        async with session.begin():
            await _user(session, message.from_user)
            class_group = await session.scalar(
                select(ClassGroup).where(
                    ClassGroup.group_link_code_hash == hash_code(code),
                    ClassGroup.group_link_code_expires_at > datetime.now(timezone.utc),
                    ClassGroup.is_active.is_(True),
                )
            )
            if class_group is None:
                await message.answer("کد اتصال نامعتبر یا قبلاً مصرف شده است.")
                return
            occupied = await session.scalar(
                select(ClassGroup.id).where(
                    ClassGroup.group_chat_id == message.chat.id,
                    ClassGroup.id != class_group.id,
                )
            )
            if occupied is not None:
                await message.answer("این گروه قبلاً به کلاس دیگری متصل شده است.")
                return
            class_group.group_chat_id = message.chat.id
            class_group.group_chat_title = (message.chat.title or "")[:180]
            class_group.group_link_code_hash = None
            class_group.group_link_code_expires_at = None
            class_name = class_group.name
    await message.answer(
        f"✅ این گروه به کلاس <b>{escape(class_name)}</b> متصل شد.\n"
        "داده‌های این گروه از سایر کلاس‌ها جدا نگهداری می‌شوند."
    )


@router.message(Command("join"))
async def join_class(message: Message):
    if message.chat.type != "private":
        await message.answer("برای عضویت در کلاس، این کد را در گفت‌وگوی خصوصی ربات وارد کن.")
        return
    try:
        code = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        await message.answer("قالب: /join کد_عضویت")
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=True)
            class_group = await session.scalar(
                select(ClassGroup).where(
                    ClassGroup.member_join_code_hash == hash_code(code),
                    ClassGroup.member_join_code_expires_at > datetime.now(timezone.utc),
                    ClassGroup.is_active.is_(True),
                )
            )
            if class_group is None:
                await message.answer("کد عضویت معتبر نیست.")
                return
            await _ensure_group_member(session, user, class_group.id)
            class_name = class_group.name
    await message.answer(f"✅ به کلاس <b>{escape(class_name)}</b> متصل شدی.", reply_markup=main_menu())


@router.message(Command("approver_add"))
async def approver_add(message: Message):
    try:
        class_id_s, telegram_id_s = _parse_pipe_args(message.text.split(maxsplit=1)[1], 2)
        class_id, telegram_id = int(class_id_s), int(telegram_id_s)
    except (IndexError, ValueError):
        await message.answer("قالب: /approver_add شناسه_کلاس | شناسه_تلگرام")
        return
    if message.chat.type != "private":
        await message.answer("این فرمان را در گفت‌وگوی خصوصی ربات اجرا کن.")
        return
    async with SessionFactory() as session:
        async with session.begin():
            actor = await _user(session, message.from_user, private=True)
            context = await get_class_with_school(session, class_id)
            if not context:
                await message.answer("کلاس پیدا نشد.")
                return
            _, _, school = context
            actor_membership = await session.scalar(
                select(ClassMembership).where(
                    ClassMembership.class_id == class_id,
                    ClassMembership.user_id == actor.id,
                    ClassMembership.role == Role.CLASS_MANAGER,
                    ClassMembership.is_active.is_(True),
                )
            )
            if not await is_school_admin(session, actor, school.id) and actor_membership is None:
                await message.answer("مجوز مدیریت این کلاس را نداری.")
                return
            target = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if target is None:
                await message.answer("این کاربر باید ابتدا ربات را در خصوصی Start کند.")
                return
            membership = await session.scalar(
                select(ClassMembership).where(
                    ClassMembership.class_id == class_id,
                    ClassMembership.user_id == target.id,
                )
            )
            if membership is None:
                membership = ClassMembership(
                    class_id=class_id, user_id=target.id, role=Role.TRUSTED_APPROVER
                )
                session.add(membership)
            else:
                membership.role = Role.TRUSTED_APPROVER
                membership.is_active = True
    await message.answer("✅ کاربر به‌عنوان فرد مورد اعتماد این کلاس تعیین شد.")


@router.message(Command("subject_add"))
async def subject_add_command(message: Message):
    args = message.text.split(maxsplit=1)
    subject_name = args[1].strip() if len(args) > 1 else ""
    if message.chat.type in {"group", "supergroup"}:
        class_id = None
    else:
        try:
            class_id_s, subject_name = _parse_pipe_args(args[1], 2)
            class_id = int(class_id_s)
        except (IndexError, ValueError):
            await message.answer("در گروه: /subject_add نام درس\nدر خصوصی: /subject_add شناسه_کلاس | نام درس")
            return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            context = await _class_from_message(session, message, class_id, user)
            if not context:
                await message.answer("کلاس مشخص نیست؛ ابتدا گروه را متصل کن یا شناسه کلاس را وارد کن.")
                return
            class_group, _, school = context
            membership = await session.scalar(
                select(ClassMembership).where(
                    ClassMembership.class_id == class_group.id,
                    ClassMembership.user_id == user.id,
                    ClassMembership.role.in_([Role.CLASS_MANAGER, Role.TRUSTED_APPROVER]),
                    ClassMembership.is_active.is_(True),
                )
            )
            if not await is_school_admin(session, user, school.id) and membership is None:
                await message.answer("فقط مدیر کلاس یا فرد مورد اعتماد می‌تواند درس اضافه کند.")
                return
            if not subject_name:
                await message.answer("نام درس را وارد کن.")
                return
            subject = await add_subject(session, class_group.id, school.id, subject_name)
            subject_id = subject.id
    await message.answer(f"✅ درس «{escape(subject_name)}» به کلاس اضافه شد. (شناسه {subject_id})")


async def _start_wizard(
    chat_id: int,
    from_user,
    kind: str,
    state: FSMContext,
    bot: Bot,
    message: Message | None = None,
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, from_user, private=chat_id == from_user.id)
            if chat_id != from_user.id:
                context = await get_group_context(session, chat_id)
                classes = [context[0]] if context else []
                if context:
                    await _ensure_group_member(session, user, context[0].id)
            else:
                classes = await _classes_for_user(session, user)
    if not classes:
        async with SessionFactory() as session:
            async with session.begin():
                user = await session.scalar(select(User).where(User.telegram_id == from_user.id))
                keyboard = (
                    await _main_keyboard_for_user(session, user)
                    if user and chat_id == from_user.id
                    else None
                )
        await bot.send_message(
            chat_id,
            "کلاسی برای این حساب پیدا نشد. از دکمه «🔑 عضویت در کلاس» استفاده کن.",
            reply_markup=keyboard,
        )
        return
    await state.clear()
    await state.update_data(record_type=kind, source_chat_id=chat_id)
    if chat_id == from_user.id:
        await bot.send_message(
            chat_id,
            "فرایند ثبت آغاز شد. برای خروج، دکمه «❌ لغو عملیات» را بزن.",
            reply_markup=wizard_cancel_keyboard(),
        )
    if len(classes) == 1:
        await _choose_class_for_wizard(classes[0].id, chat_id, state, bot)
    else:
        await state.set_state(RecordWizard.choose_class)
        await bot.send_message(chat_id, "کدام کلاس؟", reply_markup=class_picker(classes))


async def _choose_class_for_wizard(
    class_id: int, chat_id: int, state: FSMContext, bot: Bot
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            context = await get_class_with_school(session, class_id)
            subjects = await _subject_list(session, class_id)
    if not context:
        await bot.send_message(chat_id, "کلاس پیدا نشد.")
        await state.clear()
        return
    await state.update_data(class_id=class_id, timezone=context[2].timezone)
    data = await state.get_data()
    if data["record_type"] == "report":
        await state.set_state(RecordWizard.report_date)
        await bot.send_message(
            chat_id,
            "تاریخ گزارش را وارد کن؛ نمونه: ۱۴۰۵/۰۸/۲۷\nبرای گزارش امروز، «امروز» بنویس."
        )
    else:
        if not subjects:
            user = await session.scalar(select(User).where(User.telegram_id == chat_id))
            keyboard = (
                await _main_keyboard_for_user(session, user)
                if user and chat_id > 0
                else None
            )
            await bot.send_message(
                chat_id,
                "برای این کلاس هنوز درسی ثبت نشده است. مدیر کلاس باید از پنل مدیریت، درس‌ها را اضافه کند.",
                reply_markup=keyboard,
            )
            await state.clear()
            return
        await state.set_state(RecordWizard.choose_subject)
        await bot.send_message(chat_id, "درس موردنظر را انتخاب کن:", reply_markup=subject_picker(subjects))


@router.callback_query(F.data.startswith("new:"))
async def new_record_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    kind = callback.data.split(":", 1)[1]
    if kind not in {"homework", "exam", "report"}:
        await callback.answer("نوع ثبت نامعتبر است.", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _start_wizard(callback.message.chat.id, callback.from_user, kind, state, bot)


@router.message(Command("new_homework"))
async def new_homework_command(message: Message, state: FSMContext, bot: Bot):
    await _start_wizard(message.chat.id, message.from_user, "homework", state, bot)


@router.message(Command("new_exam"))
async def new_exam_command(message: Message, state: FSMContext, bot: Bot):
    await _start_wizard(message.chat.id, message.from_user, "exam", state, bot)


@router.message(Command("new_report"))
async def new_report_command(message: Message, state: FSMContext, bot: Bot):
    await _start_wizard(message.chat.id, message.from_user, "report", state, bot)


@router.callback_query(F.data.startswith("wizard:class:"))
async def wizard_class_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    try:
        class_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("شناسه کلاس نامعتبر است.", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=True)
            classes = await _classes_for_user(session, user)
    if class_id not in {item.id for item in classes}:
        await callback.answer("به این کلاس دسترسی نداری.", show_alert=True)
        return
    await callback.answer()
    await _choose_class_for_wizard(class_id, callback.message.chat.id, state, bot)


@router.callback_query(F.data.startswith("wizard:subject:"))
async def wizard_subject_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    class_id = data.get("class_id")
    try:
        subject_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("درس نامعتبر است.", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            subject = await session.scalar(
                select(Subject)
                .join(ClassSubject, ClassSubject.subject_id == Subject.id)
                .where(
                    Subject.id == subject_id,
                    ClassSubject.class_id == class_id,
                    ClassSubject.deleted_at.is_(None),
                    Subject.deleted_at.is_(None),
                )
            )
    if subject is None or class_id is None:
        await callback.answer("این درس متعلق به کلاس انتخاب‌شده نیست.", show_alert=True)
        return
    await state.update_data(subject_id=subject_id, subject_name=subject.name)
    kind = data["record_type"]
    if kind == "homework":
        await state.set_state(RecordWizard.homework_title)
        prompt = "عنوان تکلیف را وارد کن:"
    else:
        await state.set_state(RecordWizard.exam_title)
        prompt = "عنوان امتحان را وارد کن:"
    await callback.message.answer(prompt)
    await callback.answer()


async def _submit_pending(
    bot: Bot, chat_id: int, from_user, state: FSMContext, entity_type: str,
    title: str, details: str, create_fn,
) -> None:
    data = await state.get_data()
    class_id = data["class_id"]
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, from_user, private=chat_id == from_user.id)
            context = await get_class_with_school(session, class_id)
            if not context:
                await bot.send_message(chat_id, "کلاس دیگر فعال نیست.")
                await state.clear()
                return
            class_group, _, school = context
            await _ensure_group_member(session, user, class_id)
            record = await create_fn(session, user)
            await audit(
                session, school.id, class_id, user.id, "create", entity_type,
                record.id, {"title": title, "status": RecordStatus.PENDING},
            )
            record_id = record.id
            class_snapshot = class_group
            creator_name = user.full_name
            platform_admin = user.is_platform_admin
            school_admin = platform_admin or (
                await session.scalar(
                    select(SchoolMembership.id).where(
                        SchoolMembership.user_id == user.id,
                        SchoolMembership.role == Role.SCHOOL_ADMIN,
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
            class_manager = Role.CLASS_MANAGER in class_roles
            approver = school_admin or class_manager or Role.TRUSTED_APPROVER in class_roles
    await state.clear()
    await bot.send_message(
        chat_id,
        "✅ درخواست ثبت شد و تا زمان تأیید قطعی نیست.\n"
        f"شناسه درخواست: <code>{record_id}</code>",
        reply_markup=(
            main_reply_keyboard(
                is_platform_admin=platform_admin,
                is_school_admin=school_admin,
                is_class_manager=class_manager,
                is_approver=approver,
            )
            if chat_id == from_user.id
            else None
        ),
    )
    await _pending_message(
        bot, class_snapshot, entity_type, record_id, title, details, creator_name
    )


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("فرایند فعالی برای لغو وجود ندارد.")
        return
    await state.clear()
    await message.answer("فرایند ثبت لغو شد و اطلاعات نیمه‌کاره ذخیره نشد.")


@router.message(RecordWizard.homework_title)
async def wizard_homework_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title or len(title) > 180:
        await message.answer("عنوان باید بین ۱ تا ۱۸۰ نویسه باشد.")
        return
    await state.update_data(title=title)
    await state.set_state(RecordWizard.homework_description)
    await message.answer("شرح تکلیف را وارد کن:")


@router.message(RecordWizard.homework_description)
async def wizard_homework_description(message: Message, state: FSMContext):
    description = (message.text or "").strip()
    if not description or len(description) > 4000:
        await message.answer("شرح باید بین ۱ تا ۴۰۰۰ نویسه باشد.")
        return
    await state.update_data(description=description)
    await state.set_state(RecordWizard.homework_due)
    await message.answer(
        "موعد تحویل را به شکل ۱۴۰۵/۰۸/۲۷ وارد کن؛ "
        "برای جلسه بعد «جلسه بعد» و برای نامشخص «-» بنویس."
    )


@router.message(RecordWizard.homework_due)
async def wizard_homework_due(message: Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip()
    data = await state.get_data()
    title, description = data["title"], data["description"]
    subject_id = data["subject_id"]
    next_session_values = {"جلسه بعد", "جلسه بعدی", "جلسه آینده", "جلسه ی آینده"}
    due_session_id = None

    if normalize_subject_name(raw) in {
        normalize_subject_name(item) for item in next_session_values
    }:
        async with SessionFactory() as session:
            async with session.begin():
                lesson = await get_or_create_next_lesson(
                    session,
                    data["class_id"],
                    subject_id,
                    data["timezone"],
                )
                if lesson is None:
                    await message.answer(
                        "برای این درس جلسه آینده پیدا نشد. مدیر کلاس باید ابتدا "
                        "برنامه هفتگی را با /timetable_set ثبت کند."
                    )
                    return
                due_session_id = lesson.id
                due_on = lesson.starts_at.astimezone(ZoneInfo(data["timezone"])).date()
                due_text = f"جلسه بعد ({jalali_datetime(lesson.starts_at, data['timezone'])})"
    else:
        try:
            due_on = None if raw == "-" else parse_school_date(raw)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        due_text = jalali_date(due_on) if due_on else "نامشخص"

    async def create(session, user):
        subject = await session.get(Subject, subject_id)
        return await create_homework(
            session,
            data["class_id"],
            subject,
            title,
            description,
            due_on,
            user.id,
            due_session_id=due_session_id,
        )

    await _submit_pending(
        bot,
        message.chat.id,
        message.from_user,
        state,
        "homework",
        title,
        f"{description} | موعد: {due_text}",
        create,
    )


@router.message(RecordWizard.exam_title)
async def wizard_exam_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title or len(title) > 180:
        await message.answer("عنوان باید بین ۱ تا ۱۸۰ نویسه باشد.")
        return
    await state.update_data(title=title)
    await state.set_state(RecordWizard.exam_scope)
    await message.answer("محدوده امتحان را وارد کن:")


@router.message(RecordWizard.exam_scope)
async def wizard_exam_scope(message: Message, state: FSMContext):
    scope = (message.text or "").strip()
    if len(scope) > 4000:
        await message.answer("محدوده بیش از حد طولانی است.")
        return
    await state.update_data(scope=scope)
    await state.set_state(RecordWizard.exam_datetime)
    await message.answer("تاریخ و ساعت را وارد کن؛ نمونه: ۱۴۰۵/۰۸/۲۷ ۰۹:۰۰")


@router.message(RecordWizard.exam_datetime)
async def wizard_exam_datetime(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    try:
        scheduled_at = parse_school_datetime(message.text or "", data["timezone"])
        if scheduled_at <= __import__("datetime").datetime.now(ZoneInfo(data["timezone"])):
            raise ValueError("زمان امتحان باید در آینده باشد.")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    title, scope, subject_id = data["title"], data["scope"], data["subject_id"]

    async def create(session, user):
        subject = await session.get(Subject, subject_id)
        return await create_exam(
            session, data["class_id"], subject, title, scope, "exam",
            scheduled_at, user.id,
        )

    await _submit_pending(
        bot, message.chat.id, message.from_user, state, "exam", title,
        f"{scope} | {jalali_datetime(scheduled_at, data['timezone'])}", create,
    )


@router.message(RecordWizard.report_date)
async def wizard_report_date(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw == "امروز":
        data = await state.get_data()
        report_date = datetime.now(ZoneInfo(data["timezone"])).date()
    else:
        try:
            report_date = parse_school_date(raw)
        except ValueError as exc:
            await message.answer(str(exc))
            return
    await state.update_data(report_date=report_date.isoformat())
    await state.set_state(RecordWizard.report_body)
    await message.answer("گزارش اتفاقات روز را بنویس. بهتر است هر زنگ و درس را جداگانه توضیح بدهی.")


@router.message(RecordWizard.report_body)
async def wizard_report_body(message: Message, state: FSMContext, bot: Bot):
    body = (message.text or "").strip()
    if not body or len(body) > 8000:
        await message.answer("گزارش باید بین ۱ تا ۸۰۰۰ نویسه باشد.")
        return
    data = await state.get_data()
    report_date = date.fromisoformat(data["report_date"])
    title = f"گزارش روز {jalali_date(report_date)}"

    async def create(session, user):
        return await create_daily_report(
            session, data["class_id"], report_date, body, user.id, title
        )

    await _submit_pending(
        bot, message.chat.id, message.from_user, state, "report", title, body, create
    )


@router.callback_query(F.data.startswith("approval:"))
async def approval_callback(callback: CallbackQuery, bot: Bot):
    try:
        _, action, entity_type, entity_id_raw = callback.data.split(":")
        entity_id = int(entity_id_raw)
    except ValueError:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    if action not in {"approve", "reject"} or entity_type not in {"homework", "exam", "report"}:
        await callback.answer("عملیات نامعتبر است.", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=callback.message.chat.type == "private")
            context = await get_group_context(session, callback.message.chat.id)
            if not context:
                await callback.answer("این گروه به کلاسی متصل نیست.", show_alert=True)
                return
            class_group, _, school = context
            if not await can_approve(session, user, class_group.id):
                await callback.answer("فقط افراد مورد اعتماد می‌توانند تأیید کنند.", show_alert=True)
                return
            if action == "approve":
                success, result_text = await approve_record(
                    session, entity_type, entity_id, user.id, school.id,
                    class_group.id, school.timezone,
                )
            else:
                success, result_text = await reject_record(
                    session, entity_type, entity_id, user.id, school.id,
                    class_group.id, "رد شده توسط فرد مورد اعتماد",
                )
    await callback.answer(result_text, show_alert=not success)
    if success:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        if action == "approve":
            await bot.send_message(
                class_group.group_chat_id,
                f"✅ <b>اطلاعات تأیید شد</b>\nنوع: {ENTITY_LABELS.get(entity_type, 'اطلاعات')}\nشناسه: {entity_id}\n"
                "این مورد اکنون در جست‌وجو و فهرست اطلاعات کلاس قابل مشاهده است."
            )
        else:
            await bot.send_message(
                class_group.group_chat_id,
                f"❌ درخواست {ENTITY_LABELS.get(entity_type, 'اطلاعات')} با شناسه {entity_id} رد شد."
            )


@router.message(Command("cancel_record"))
async def cancel_record_command(message: Message):
    """Cancel an approved record in a linked group or by class ID in private chat."""
    parts = (message.text or "").split()
    if message.chat.type == "private":
        if len(parts) != 4 or not parts[1].isdigit():
            await message.answer("در خصوصی: /cancel_record شناسه_کلاس homework|exam|report شناسه_رکورد")
            return
        class_id, entity_type, entity_id_raw = int(parts[1]), parts[2].lower(), parts[3]
    else:
        if len(parts) != 3:
            await message.answer("در گروه: /cancel_record homework|exam|report شناسه_رکورد")
            return
        class_id, entity_type, entity_id_raw = None, parts[1].lower(), parts[2]
    if entity_type not in {EntityType.HOMEWORK, EntityType.EXAM, EntityType.REPORT} or not entity_id_raw.isdigit():
        await message.answer("نوع یا شناسه رکورد معتبر نیست.")
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            context = await _class_from_message(session, message, class_id, user)
            if not context:
                await message.answer("کلاس مشخص نیست یا گروه به کلاس متصل نیست.")
                return
            class_group, _, school = context
            if not await can_approve(session, user, class_group.id):
                await message.answer("برای لغو اطلاعات تأییدشده مجوز نداری.")
                return
            _, response = await cancel_approved_record(
                session, entity_type, int(entity_id_raw), user.id,
                school.id, class_group.id,
            )
    await message.answer(response)


@router.message(Command("pending"))
async def pending_command(message: Message):
    class_id = None
    parts = message.text.split()
    if message.chat.type == "private":
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("در خصوصی: /pending شناسه_کلاس")
            return
        class_id = int(parts[1])
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            context = await _class_from_message(session, message, class_id, user)
            if not context:
                await message.answer("کلاس مشخص نیست.")
                return
            class_group, _, school = context
            if not await can_approve(session, user, class_group.id):
                await message.answer("فقط افراد مورد اعتماد می‌توانند صف بررسی را ببینند.")
                return
            pending = await list_pending_records(session, class_group.id)
    if not pending:
        await message.answer("درخواستی در انتظار بررسی نیست.")
        return
    for kind, record in pending:
        title = getattr(record, "title", "گزارش روزانه")
        body = getattr(record, "description", getattr(record, "body", getattr(record, "scope", "")))
        await message.answer(
            f"🕓 <b>{escape(ENTITY_LABELS.get(kind, 'اطلاعات'))}</b> #{record.id}\n{escape(title)}\n{escape(body[:1200])}",
            reply_markup=approval_keyboard(kind, record.id),
        )


@router.message(Command("reminder_uncertain"))
async def reminder_uncertain_command(message: Message):
    """List reminder sends whose delivery outcome could not be determined."""
    async with SessionFactory() as session:
        async with session.begin():
            actor = await _user(session, message.from_user, private=message.chat.type == "private")
            if not actor.is_platform_admin:
                await message.answer("این فرمان فقط برای مدیر کل در دسترس است.")
                return
            rows = (
                await session.execute(
                    select(ReminderJob)
                    .where(ReminderJob.status == "uncertain")
                    .order_by(ReminderJob.id.desc())
                    .limit(20)
                )
            ).scalars().all()
    if not rows:
        await message.answer("یادآوری نامطمئنی برای بررسی وجود ندارد.")
        return
    lines = ["⚠️ <b>یادآوری‌های با نتیجه ارسال نامشخص</b>"]
    for job in rows:
        lines.append(
            f"\n• شناسه <code>{job.id}</code> | مقصد {job.target_type} | تلاش {job.attempts}\n"
            f"  {escape(job.last_error or 'علت ثبت نشده')[:240]}"
        )
    lines.append(
        "\nبرای علامت‌گذاری به‌عنوان ارسال‌شده: <code>/reminder_resolve شناسه sent</code>\n"
        "برای ارسال مجدد: <code>/reminder_resolve شناسه retry</code>؛ ارسال مجدد ممکن است پیام تکراری بسازد."
    )
    await message.answer("\n".join(lines))


@router.message(Command("reminder_resolve"))
async def reminder_resolve_command(message: Message):
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"sent", "retry"}:
        await message.answer("قالب: /reminder_resolve شناسه sent|retry")
        return
    async with SessionFactory() as session:
        async with session.begin():
            actor = await _user(session, message.from_user, private=message.chat.type == "private")
            if not actor.is_platform_admin:
                await message.answer("این فرمان فقط برای مدیر کل در دسترس است.")
                return
            job = await session.get(ReminderJob, int(parts[1]), with_for_update=True)
            if job is None or job.status != "uncertain":
                await message.answer("یادآوری نامطمئن با این شناسه پیدا نشد.")
                return
            if parts[2] == "sent":
                job.status = "sent"
                job.sent_at = datetime.now(timezone.utc)
                job.last_error = "Manually reconciled by platform administrator as already delivered."
                response = "یادآوری به‌عنوان ارسال‌شده ثبت شد و دیگر ارسال نمی‌شود."
            else:
                job.status = "retry"
                job.scheduled_at = datetime.now(timezone.utc)
                job.locked_at = None
                job.last_error = "Manually re-queued by platform administrator; duplicate delivery is possible."
                response = "یادآوری برای ارسال مجدد در صف قرار گرفت؛ احتمال پیام تکراری وجود دارد."
    await message.answer(response)


@router.message(Command("homework"))
async def homework_command(message: Message):
    class_id = None
    subject_name = None
    parts = message.text.split(maxsplit=2)
    if message.chat.type == "private":
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("در خصوصی: /homework شناسه_کلاس [نام درس]")
            return
        class_id = int(parts[1])
        subject_name = parts[2] if len(parts) > 2 else None
    elif len(parts) > 1:
        subject_name = parts[1]
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            context = await _class_from_message(session, message, class_id, user)
            if not context:
                await message.answer("کلاس مشخص نیست یا گروه متصل نیست.")
                return
            class_group = context[0]
            rows = await list_homework(session, class_group.id, subject_name)
    await message.answer(_format_homework(rows))


@router.message(Command("exams"))
async def exams_command(message: Message):
    class_id = None
    parts = message.text.split()
    if message.chat.type == "private":
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("در خصوصی: /exams شناسه_کلاس")
            return
        class_id = int(parts[1])
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            context = await _class_from_message(session, message, class_id, user)
            if not context:
                await message.answer("کلاس مشخص نیست یا گروه متصل نیست.")
                return
            class_group, _, school = context
            rows = await list_exams(session, class_group.id)
    await message.answer(_format_exams(rows, school.timezone))


@router.message(Command("today"))
async def today_command(message: Message):
    class_id = None
    parts = message.text.split()
    if message.chat.type == "private":
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("در خصوصی: /today شناسه_کلاس")
            return
        class_id = int(parts[1])
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            context = await _class_from_message(session, message, class_id, user)
            if not context:
                await message.answer("کلاس مشخص نیست.")
                return
            class_group, _, school = context
            today = datetime.now(ZoneInfo(school.timezone)).date()
            reports = await list_reports(session, class_group.id, today)
    if not reports:
        await message.answer("برای امروز گزارش تأییدشده‌ای ثبت نشده است.")
    else:
        for report in reports:
            await message.answer(
                f"🗒 <b>{escape(report.title or 'گزارش روزانه')}</b>\n"
                f"{jalali_date(report.report_date)}\n{escape(report.body)}"
            )



@router.message(Command("timetable_set"))
async def timetable_set_command(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "قالب در گروه: /timetable_set روز | زنگ | نام درس | شروع-پایان\n"
            "قالب در خصوصی: /timetable_set شناسه_کلاس | روز | زنگ | نام درس | شروع-پایان"
        )
        return
    class_id = None
    try:
        if message.chat.type == "private":
            class_id_text, day_text, period_text, subject_name, time_range = _parse_pipe_args(parts[1], 5)
            class_id = int(class_id_text)
        else:
            day_text, period_text, subject_name, time_range = _parse_pipe_args(parts[1], 4)
        day_aliases = {
            "شنبه": 0, "یکشنبه": 1, "دوشنبه": 2, "سه‌شنبه": 3,
            "چهارشنبه": 4, "پنجشنبه": 5, "جمعه": 6,
        }
        day_text = day_text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
        weekday = day_aliases.get(day_text, int(day_text) if day_text.isdigit() else -1)
        period = int(period_text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
        if weekday not in range(7) or period < 1:
            raise ValueError
        if time_range == "-":
            starts_at = ends_at = None
        else:
            start_raw, end_raw = time_range.split("-", 1)
            sh, sm = (int(part) for part in start_raw.split(":"))
            eh, em = (int(part) for part in end_raw.split(":"))
            starts_at, ends_at = time(sh, sm), time(eh, em)
            if starts_at >= ends_at:
                raise ValueError
    except (ValueError, TypeError):
        await message.answer("روز، زنگ یا ساعت نامعتبر است. نمونه: /timetable_set شنبه | ۱ | ریاضی | 08:00-08:45")
        return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            context = await _class_from_message(session, message, class_id, user)
            if not context:
                await message.answer("کلاس مشخص نیست یا به آن دسترسی نداری.")
                return
            class_group, _, school = context
            manager = await session.scalar(
                select(ClassMembership.id).where(
                    ClassMembership.class_id == class_group.id,
                    ClassMembership.user_id == user.id,
                    ClassMembership.role == Role.CLASS_MANAGER,
                    ClassMembership.is_active.is_(True),
                )
            )
            if not await is_school_admin(session, user, school.id) and manager is None:
                await message.answer("فقط مدیر مدرسه یا مدیر کلاس می‌تواند برنامه را تغییر دهد.")
                return
            subject = await get_subject(session, class_group.id, school.id, subject_name)
            if subject is None:
                await message.answer("این درس برای کلاس تعریف نشده است؛ ابتدا /subject_add را اجرا کن.")
                return
            entry = await session.scalar(
                select(TimetableEntry).where(
                    TimetableEntry.class_id == class_group.id,
                    TimetableEntry.weekday == weekday,
                    TimetableEntry.period == period,
                )
            )
            if entry is None:
                entry = TimetableEntry(
                    class_id=class_group.id, subject_id=subject.id, weekday=weekday,
                    period=period, starts_at=starts_at, ends_at=ends_at,
                )
                session.add(entry)
                await session.flush()
            else:
                entry.deleted_at = None
                entry.subject_id = subject.id
                entry.starts_at = starts_at
                entry.ends_at = ends_at
            await audit(
                session, school.id, class_group.id, user.id, "timetable_set",
                "timetable_entry", entry.id,
                {"weekday": weekday, "period": period, "subject": subject.name},
            )
    await message.answer(
        f"✅ برنامه ثبت شد: {WEEKDAYS[weekday]}، زنگ {period}، {escape(subject_name)}"
    )

@router.message(Command("week"))
async def week_command(message: Message):
    class_id = None
    parts = message.text.split()
    if message.chat.type == "private":
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("در خصوصی: /week شناسه_کلاس")
            return
        class_id = int(parts[1])
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=message.chat.type == "private")
            context = await _class_from_message(session, message, class_id, user)
            if not context:
                await message.answer("کلاس مشخص نیست.")
                return
            class_group = context[0]
            entries = list(
                (
                    await session.execute(
                        select(TimetableEntry, Subject)
                        .join(Subject, TimetableEntry.subject_id == Subject.id)
                        .where(TimetableEntry.class_id == class_group.id)
                        .order_by(TimetableEntry.weekday, TimetableEntry.period)
                    )
                ).all()
            )
    if not entries:
        await message.answer("برنامه هفتگی هنوز برای این کلاس ثبت نشده است.")
        return
    lines = ["📅 <b>برنامه هفتگی</b>"]
    for entry, subject in entries:
        slot = f"زنگ {entry.period}"
        if entry.starts_at:
            slot += f" ({entry.starts_at.strftime('%H:%M')})"
        lines.append(f"{WEEKDAYS[entry.weekday]} — {slot}: {escape(subject.name)}")
    await message.answer("\n".join(lines))


@router.message(Command("ask"))
async def ask_command(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    query_text = parts[1].strip() if len(parts) > 1 else ""
    if not query_text:
        await message.answer("نمونه: /ask برای جلسه بعد نگارش چی داریم؟")
        return
    class_id = None
    if message.chat.type == "private":
        try:
            class_id_text, query_text = _parse_pipe_args(query_text, 2)
            class_id = int(class_id_text)
        except (ValueError, TypeError):
            await message.answer("در خصوصی: /ask شناسه_کلاس | عبارت")
            return
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(
                session, message.from_user, private=message.chat.type == "private"
            )
            if message.chat.type in {"group", "supergroup"}:
                context = await get_group_context(session, message.chat.id)
            else:
                context = await _class_from_message(session, message, class_id, user)
            if not context:
                await message.answer("کلاس مشخص نیست یا به آن دسترسی نداری.")
                return
            class_group, _, school = context
            normalized = normalize_subject_name(query_text)
            if any(word in normalized for word in ("امتحان", "آزمون", "کوئیز")):
                rows = await list_exams(session, class_group.id)
                answer = _format_exams(rows, school.timezone)
            else:
                next_session_phrases = (
                    "برای جلسه آینده",
                    "برای جلسه بعدی",
                    "برای جلسه بعد",
                    "جلسه آینده",
                    "جلسه بعدی",
                    "جلسه بعد",
                )
                asks_next_session = any(
                    phrase in normalized for phrase in next_session_phrases
                )
                subject_query = normalized
                for phrase in (
                    *next_session_phrases,
                    "چی داریم",
                    "چه داریم",
                    "چه تکلیفی داریم",
                    "تکلیف",
                    "درس",
                ):
                    subject_query = subject_query.replace(phrase, " ")
                for punctuation in ("؟", "?", "!", "،", ",", ":", ";", "."):
                    subject_query = subject_query.replace(punctuation, " ")
                subject_query = " ".join(subject_query.split())

                if asks_next_session:
                    subject = await get_subject(
                        session, class_group.id, school.id, subject_query
                    )
                    if subject is None:
                        answer = (
                            "نام درس را از فهرست درس‌های این کلاس تشخیص ندادم. "
                            "نمونه: /ask برای جلسه بعد نگارش چی داریم؟"
                        )
                    else:
                        lesson = await get_or_create_next_lesson(
                            session, class_group.id, subject.id, school.timezone
                        )
                        if lesson is None:
                            answer = (
                                f"برای درس {escape(subject.name)} جلسه آینده‌ای پیدا نشد. "
                                "مدیر کلاس باید برنامه هفتگی را با /timetable_set ثبت کند."
                            )
                        else:
                            lesson_time = jalali_datetime(
                                lesson.starts_at, school.timezone
                            )
                            rows = await list_homework_for_lesson(
                                session, class_group.id, subject.id, lesson
                            )
                            if rows:
                                answer = (
                                    f"📅 <b>جلسه بعد {escape(subject.name)}</b>\n"
                                    f"زمان: {lesson_time}\n\n"
                                    f"{_format_homework(rows)}"
                                )
                            else:
                                answer = (
                                    f"📅 <b>جلسه بعد {escape(subject.name)}</b>\n"
                                    f"زمان: {lesson_time}\n"
                                    "فعلاً تکلیف تأییدشده‌ای برای این جلسه ثبت نشده است."
                                )
                else:
                    rows = await list_homework(
                        session, class_group.id, subject_query or None
                    )
                    answer = _format_homework(rows)
    await message.answer(
        answer + "\n\n<i>پاسخ بر اساس اطلاعات تأییدشده ثبت‌شده در این کلاس است.</i>"
    )


@router.message(Command("reminders_off", "reminders_on"))
async def reminder_preferences(message: Message):
    if message.chat.type != "private":
        await message.answer("تنظیم یادآوری خصوصی را در گفت‌وگوی خصوصی ربات انجام بده.")
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "قالب: /reminders_on شناسه_کلاس [homework|exams|all]\n"
            "برای خاموش‌کردن از /reminders_off استفاده کن."
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
            preference = await session.scalar(
                select(NotificationPreference).where(
                    NotificationPreference.user_id == user.id,
                    NotificationPreference.class_id == class_id,
                )
            )
            if preference is None:
                preference = NotificationPreference(user_id=user.id, class_id=class_id)
                session.add(preference)
            if category in {"homework", "all"}:
                preference.homework_enabled = enabled
            if category in {"exams", "all"}:
                preference.exams_enabled = enabled
    state_label = "فعال" if enabled else "غیرفعال"
    await message.answer(f"✅ یادآوری‌های انتخاب‌شده برای این کلاس {state_label} شدند.")
