import logging
from datetime import datetime, time, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommandScopeChat, CallbackQuery, Message
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.bot.formatters import _format_exams, _format_homework
from classmate.bot.keyboards import (
    main_menu,
    main_reply_keyboard,
    new_record_menu,
)
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
from classmate.domain.enums import Role
from classmate.domain.dates import (
    jalali_date,
    jalali_datetime,
)
from classmate.services.permissions import (
    ensure_user,
    get_class_with_school,
    hash_code,
    is_school_admin,
    make_code,
    user_can_access_class,
)
from classmate.services.records import (
    add_subject,
    audit,
    get_group_context,
    get_subject,
    get_or_create_next_lesson,
    list_exams,
    list_homework,
    list_homework_for_lesson,
    list_reports,
    normalize_subject_name,
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
            try:
                subject = await add_subject(session, class_group.id, school.id, subject_name)
            except ValueError as exc:
                await message.answer(str(exc))
                return
            subject_id = subject.id
    await message.answer(f"✅ درس «{escape(subject_name)}» به کلاس اضافه شد. (شناسه {subject_id})")


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
                        .where(
                            TimetableEntry.class_id == class_group.id,
                            TimetableEntry.deleted_at.is_(None),
                            Subject.deleted_at.is_(None),
                        )
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
