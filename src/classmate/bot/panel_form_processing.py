from __future__ import annotations

from datetime import datetime, time, timedelta
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, Message
from sqlalchemy import select

from classmate.bot.context import WEEKDAYS, _ensure_group_member, _parse_pipe_args, _user
from classmate.bot.keyboards import main_reply_keyboard
from classmate.bot.states import PanelInput
from classmate.bot.formatters import _format_exams, _format_homework
from classmate.core.config import get_settings
from classmate.db.models import (
    AcademicYear,
    ClassGroup,
    ClassMembership,
    School,
    SchoolMembership,
    TimetableEntry,
    User,
)
from classmate.db.session import SessionFactory
from classmate.domain.dates import jalali_datetime
from classmate.domain.enums import Role
from classmate.services.permissions import (
    get_class_with_school,
    hash_code,
    is_school_admin,
    can_manage_class,
    make_code,
)
from classmate.services.records import (
    normalize_subject_name,
    add_subject,
    audit,
    get_or_create_next_lesson,
    get_subject,
    list_exams,
    list_homework,
    list_homework_for_lesson,
)


def _parse_persian_int(value: str) -> int:
    translated = value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    return int(translated)


async def _update_input_prompt(
    message: Message, state: FSMContext, bot: Bot, text: str
) -> None:
    """Advance a valid form step without replacing the reply-keyboard back control."""
    data = await state.get_data()
    prompt_id = data.get("panel_message_id")
    if prompt_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=prompt_id,
                text=text,
            )
            return
        except TelegramBadRequest:
            pass
    sent = await bot.send_message(message.chat.id, text)
    await state.update_data(panel_message_id=sent.message_id)


async def _send_join_retry_message(bot: Bot, chat_id: int, error: Exception) -> None:
    """Send join-code errors as a new message without replacing the original prompt."""
    await bot.send_message(
        chat_id,
        f"⚠️ {escape(str(error))}\n\nکد را دوباره بررسی و وارد کن.",
    )


async def handle_panel_input(
    message: Message, state: FSMContext, bot: Bot, *, render_from_message, markup
) -> None:
    raw = (message.text or "").strip()
    data = await state.get_data()
    form_name = data.get("panel_form")
    class_id = data.get("panel_class_id")
    school_id = data.get("panel_school_id")
    await state.set_state(None)
    if not form_name:
        await render_from_message(message, state, bot, "این فرم دیگر فعال نیست.", markup([]))
        return

    if form_name == "school_new" and not data.get("school_name"):
        if not raw or len(raw) > 180:
            await state.set_state(PanelInput.input)
            await bot.send_message(
                message.chat.id,
                "⚠️ نام مدرسه باید بین ۱ تا ۱۸۰ نویسه باشد. دوباره وارد کن.",
            )
            return
        await state.update_data(school_name=raw)
        await state.set_state(PanelInput.input)
        await _update_input_prompt(
            message, state, bot, "سال تحصیلی را وارد کن.\nمثال: ۱۴۰۵-۱۴۰۶"
        )
        return

    school_group_token = None
    school_group_url = None
    join_reply_keyboard = None
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
                    has_school_admin_role = actor.is_platform_admin or (
                        await session.scalar(
                            select(SchoolMembership.id).where(
                                SchoolMembership.user_id == actor.id,
                                SchoolMembership.role == Role.SCHOOL_ADMIN,
                                SchoolMembership.is_active.is_(True),
                            )
                        )
                    ) is not None
                    member_roles = set(
                        (
                            await session.scalars(
                                select(ClassMembership.role).where(
                                    ClassMembership.user_id == actor.id,
                                    ClassMembership.is_active.is_(True),
                                )
                            )
                        ).all()
                    )
                    is_class_manager = Role.CLASS_MANAGER in member_roles
                    join_reply_keyboard = main_reply_keyboard(
                        is_platform_admin=actor.is_platform_admin,
                        is_school_admin=bool(has_school_admin_role),
                        is_class_manager=is_class_manager,
                        is_approver=(
                            bool(has_school_admin_role)
                            or is_class_manager
                            or Role.TRUSTED_APPROVER in member_roles
                        ),
                    )
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
                            role=Role.SCHOOL_ADMIN,
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
                    school_raw, telegram_raw = _parse_pipe_args(raw, 2)
                    school_id, telegram_id = int(school_raw), int(telegram_raw)
                    if not actor.is_platform_admin:
                        raise PermissionError("فقط مدیر کل می‌تواند مدیر مدرسه تعیین کند.")
                    school = await session.get(School, school_id)
                    target = await session.scalar(select(User).where(User.telegram_id == telegram_id))
                    if not school or not target:
                        raise ValueError("مدرسه یا کاربر پیدا نشد. کاربر باید ابتدا ربات را Start کرده باشد.")
                    membership = await session.scalar(
                        select(SchoolMembership).where(
                            SchoolMembership.school_id == school_id,
                            SchoolMembership.user_id == target.id,
                        )
                    )
                    if membership is None:
                        session.add(SchoolMembership(school_id=school_id, user_id=target.id, role=Role.SCHOOL_ADMIN))
                    else:
                        membership.role = Role.SCHOOL_ADMIN
                        membership.is_active = True
                    result_text = "✅ مدیر مدرسه تعیین شد."
                elif form_name == "year_new":
                    if school_id is None:
                        school_raw, year_name = _parse_pipe_args(raw, 2)
                        selected_school_id = int(school_raw)
                    else:
                        year_name = raw.strip()
                        selected_school_id = int(school_id)
                    if not year_name:
                        raise ValueError("نام سال تحصیلی را وارد کن.")
                    school_id = selected_school_id
                    if not await is_school_admin(session, actor, school_id):
                        raise PermissionError("مجوز مدیریت این مدرسه را نداری.")
                    school = await session.get(School, school_id)
                    if not school:
                        raise ValueError("مدرسه پیدا نشد.")
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
                        school_raw, grade, section, name = _parse_pipe_args(raw, 4)
                        selected_school_id = int(school_raw)
                    else:
                        grade, section, name = _parse_pipe_args(raw, 3)
                        selected_school_id = int(school_id)
                    school_id = selected_school_id
                    school = await session.get(School, school_id)
                    if not school or not await is_school_admin(session, actor, school_id):
                        raise PermissionError("مدرسه پیدا نشد یا مجوز مدیریت آن را نداری.")
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
                        section=None if section == "-" else section[:32],
                    )
                    session.add(item)
                    await session.flush()
                    session.add(ClassMembership(class_id=item.id, user_id=actor.id, role=Role.CLASS_MANAGER))
                    result_text = f"✅ کلاس ایجاد شد. شناسه کلاس: <code>{item.id}</code>"
                elif form_name == "class_manager_add":
                    if class_id is None:
                        class_raw, telegram_raw = _parse_pipe_args(raw, 2)
                        target_class_id = int(class_raw)
                    else:
                        target_class_id = int(class_id)
                        telegram_raw = raw.strip()
                    telegram_id = int(telegram_raw)
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
                        session.add(ClassMembership(class_id=target_class_id, user_id=target.id, role=Role.CLASS_MANAGER))
                    else:
                        membership.role = Role.CLASS_MANAGER
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
                            ClassMembership.role == Role.CLASS_MANAGER,
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
                        session.add(ClassMembership(class_id=class_id, user_id=target.id, role=Role.TRUSTED_APPROVER))
                    else:
                        membership.role = Role.TRUSTED_APPROVER
                        membership.is_active = True
                    result_text = "✅ فرد مورد اعتماد به این کلاس اضافه شد."
                elif form_name == "subject_add":
                    if class_id is None:
                        raise ValueError("شناسه کلاس مشخص نیست.")
                    context = await get_class_with_school(session, class_id)
                    if not context:
                        raise ValueError("کلاس پیدا نشد.")
                    class_group, _, school = context
                    if not await can_manage_class(session, actor, class_id):
                        raise PermissionError("فقط مدیر مدرسه یا مدیر کلاس می‌تواند درس اضافه کند.")
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
                            ClassMembership.role == Role.CLASS_MANAGER,
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
                        entry.deleted_at = None
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
        await state.update_data(panel_form=None, panel_school_id=None, school_name=None)
        if form_name == "join":
            panel_message_id = data.get("panel_message_id")
            if panel_message_id is not None:
                try:
                    await bot.delete_message(message.chat.id, panel_message_id)
                except TelegramBadRequest:
                    pass
            await state.clear()
            await bot.send_message(
                message.chat.id,
                result_text,
                reply_markup=join_reply_keyboard or main_reply_keyboard(),
            )
            return
        if class_id:
            back = f"ui:manage:{class_id}"
        elif school_id:
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
        await render_from_message(
            message, state, bot, result_text, markup(result_rows, back)
        )
    except (ValueError, PermissionError, TypeError, KeyError) as exc:
        await state.set_state(PanelInput.input)
        await bot.send_message(
            message.chat.id,
            f"⚠️ {escape(str(exc))}\n\nاطلاعات را دوباره بررسی و وارد کن.",
        )


async def handle_search_input(
    message: Message, state: FSMContext, bot: Bot, *, render_from_message, markup, class_context
) -> None:
    query_text = (message.text or "").strip()
    data = await state.get_data()
    class_id = data.get("panel_class_id")
    context = await class_context(int(class_id or 0), message.from_user)
    if not context:
        await state.set_state(None)
        await render_from_message(message, state, bot, "به این کلاس دسترسی نداری.", markup([]))
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
    await render_from_message(
        message, state, bot,
        f"🔎 <b>نتیجه جست‌وجو</b>\n{answer}\n\n"
        "<i>نتیجه فقط از اطلاعات تأییدشده کلاس استخراج شده است.</i>",
        markup([], f"ui:class:{class_group.id}"),
    )
