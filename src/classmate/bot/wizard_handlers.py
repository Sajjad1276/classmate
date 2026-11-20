from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from classmate.bot.context import (
    _classes_for_user,
    _ensure_group_member,
    _main_keyboard_for_user,
    _subject_list,
    _user,
)
from classmate.bot.formatters import _pending_message
from classmate.bot.keyboards import (
    class_picker,
    main_reply_keyboard,
    subject_picker,
    wizard_cancel_keyboard,
    wizard_inline_cancel_keyboard,
)
from classmate.bot.states import RecordWizard
from classmate.db.models import (
    ClassMembership,
    ClassSubject,
    SchoolMembership,
    Subject,
    User,
)
from classmate.db.session import SessionFactory
from classmate.domain.dates import (
    jalali_date,
    jalali_datetime,
    parse_school_date,
    parse_school_datetime,
)
from classmate.domain.enums import RecordStatus, Role
from classmate.services.permissions import get_class_with_school
from classmate.services.records import (
    audit,
    create_daily_report,
    create_exam,
    create_homework,
    get_group_context,
    get_or_create_next_lesson,
    normalize_subject_name,
)

router = Router(name="classmate_wizards")
WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]


def _wizard_input_markup(chat_id: int, user_id: int):
    """Private chats use the persistent reply back button; groups use owner-bound inline cancel."""
    return wizard_inline_cancel_keyboard(user_id) if chat_id != user_id else None


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
    await state.update_data(
        record_type=kind,
        source_chat_id=chat_id,
        wizard_owner_id=from_user.id,
    )
    if chat_id == from_user.id:
        await bot.send_message(
            chat_id,
            "فرایند ثبت آغاز شد. برای بازگشت، دکمه «⬅️ بازگشت» را بزن.",
            reply_markup=wizard_cancel_keyboard(),
        )
    if len(classes) == 1:
        await _choose_class_for_wizard(classes[0].id, chat_id, state, bot)
    else:
        await state.set_state(RecordWizard.choose_class)
        await bot.send_message(
            chat_id,
            "کدام کلاس؟",
            reply_markup=class_picker(
                classes,
                cancel_user_id=from_user.id if chat_id != from_user.id else None,
            ),
        )


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
    owner_id = int(data.get("wizard_owner_id") or 0)
    prompt_markup = _wizard_input_markup(chat_id, owner_id) if owner_id else None
    if data["record_type"] == "report":
        await state.set_state(RecordWizard.report_date)
        await bot.send_message(
            chat_id,
            "تاریخ گزارش را وارد کن؛ نمونه: ۱۴۰۵/۰۸/۲۷\nبرای گزارش امروز، «امروز» بنویس.",
            reply_markup=prompt_markup,
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
        await bot.send_message(
            chat_id,
            "درس موردنظر را انتخاب کن:",
            reply_markup=subject_picker(
                subjects,
                cancel_user_id=owner_id if chat_id != owner_id else None,
            ),
        )


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
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
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
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await state.update_data(subject_id=subject_id, subject_name=subject.name)
    kind = data["record_type"]
    if kind == "homework":
        await state.set_state(RecordWizard.homework_title)
        prompt = "عنوان تکلیف را وارد کن:"
    else:
        await state.set_state(RecordWizard.exam_title)
        prompt = "عنوان امتحان را وارد کن:"
    await callback.message.answer(
        prompt,
        reply_markup=_wizard_input_markup(callback.message.chat.id, callback.from_user.id),
    )
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


@router.callback_query(F.data.startswith("wizard:cancel:"))
async def wizard_inline_cancel(callback: CallbackQuery, state: FSMContext):
    try:
        owner_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, TypeError, ValueError):
        await callback.answer("درخواست لغو نامعتبر است.", show_alert=True)
        return
    if callback.from_user.id != owner_id:
        await callback.answer("این دکمه متعلق به فردی است که فرایند را آغاز کرده.", show_alert=True)
        return
    if await state.get_state() is None:
        await callback.answer("این فرایند قبلاً پایان یافته است.", show_alert=True)
        return
    await state.clear()
    try:
        await callback.message.edit_text(
            "فرایند ثبت توسط شما لغو شد. برای نمایش دوباره منوی کلاس، /start را بزن.",
            reply_markup=None,
        )
    except TelegramBadRequest:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    await callback.answer("فرایند ثبت لغو شد.")


@router.message(StateFilter(RecordWizard), F.text == "⬅️ بازگشت")
async def wizard_back_button(message: Message, state: FSMContext):
    """Cancel any active record wizard before field handlers consume the back label."""
    await state.clear()
    keyboard = None
    if message.chat.type == "private":
        async with SessionFactory() as session:
            async with session.begin():
                user = await _user(session, message.from_user, private=True)
                keyboard = await _main_keyboard_for_user(session, user)
    await message.answer(
        "فرایند ثبت لغو شد؛ اطلاعات نیمه‌کاره ذخیره نشد.",
        reply_markup=keyboard,
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
