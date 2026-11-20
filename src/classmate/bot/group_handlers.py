import logging
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import or_, select

from classmate.bot.context import _user
from classmate.db.models import ClassGroup, School, Subject, TimetableEntry
from classmate.db.session import SessionFactory
from classmate.bot.context import WEEKDAYS, _user
from classmate.bot.formatters import _format_exams, _format_homework
from classmate.bot.keyboards import approval_keyboard
from classmate.domain.dates import jalali_date
from classmate.domain.enums import ENTITY_LABELS
from classmate.services.permissions import can_approve, hash_code, is_school_admin
from classmate.services.records import (
    get_group_context,
    list_exams,
    list_homework,
    list_pending_records,
    list_reports,
)
from classmate.services.group_bindings import (
    lock_group_binding_mutations,
    is_chat_bound_to_class_group,
    is_chat_bound_to_school_group,
)

logger = logging.getLogger(__name__)
router = Router(name="classmate_groups")


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.regexp(r"^/start(?:@\w+)?\s+CM_[A-Z0-9_-]{1,32}$"),
)
async def connect_school_group(message: Message, bot: Bot):
    """Consume a one-time startgroup payload and bind this Telegram group to a school."""
    if message.from_user is None or message.text is None:
        return
    try:
        bot_member = await bot.get_chat_member(message.chat.id, bot.id)
        if bot_member.status not in {"administrator", "creator"}:
            await message.answer("برای اتصال گروه مدرسه، ابتدا ربات را مدیر این گروه کن.")
            return
    except Exception:
        await message.answer("وضعیت مدیریتی ربات در این گروه قابل بررسی نیست. ابتدا ربات را مدیر کن.")
        return
    try:
        actor_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if actor_member.status not in {"administrator", "creator"}:
            await message.answer("فقط مدیر گروه می‌تواند اتصال گروه مدرسه را انجام دهد.")
            return
    except Exception:
        await message.answer("امکان بررسی دسترسی مدیر گروه وجود ندارد.")
        return
    payload = message.text.split(maxsplit=1)[1].strip()

    async with SessionFactory() as session:
        async with session.begin():
            actor = await _user(session, message.from_user, private=False)
            await lock_group_binding_mutations(session)
            now = datetime.now(timezone.utc)
            school = await session.scalar(
                select(School).where(
                    School.group_link_token_hash == hash_code(payload),
                    School.group_link_token_expires_at > now,
                    School.is_active.is_(True),
                ).with_for_update()
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
            if await is_chat_bound_to_class_group(session, message.chat.id):
                await message.answer("این گروه قبلاً به یک کلاس متصل شده است و نمی‌تواند گروه اصلی مدرسه باشد.")
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
        bot_member = await bot.get_chat_member(message.chat.id, bot.id)
        if bot_member.status not in {"administrator", "creator"}:
            await message.answer("برای اتصال کلاس، ابتدا ربات را مدیر همین گروه کن و سپس دوباره تلاش کن.")
            return
    except Exception:
        await message.answer("وضعیت مدیریتی ربات در این گروه قابل بررسی نیست. ابتدا ربات را مدیر کن.")
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
            await lock_group_binding_mutations(session)
            await _user(session, message.from_user)
            class_group = await session.scalar(
                select(ClassGroup).where(
                    ClassGroup.group_link_code_hash == hash_code(code),
                    ClassGroup.group_link_code_expires_at > datetime.now(timezone.utc),
                    ClassGroup.is_active.is_(True),
                ).with_for_update()
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
            if await is_chat_bound_to_school_group(session, message.chat.id):
                await message.answer("این گروه قبلاً گروه اصلی یک مدرسه است و نمی‌تواند به کلاس متصل شود.")
                return
            if class_group.group_chat_id not in {None, message.chat.id}:
                await message.answer("برای این کلاس قبلاً گروه دیگری متصل شده است.")
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


@router.callback_query(F.data.startswith("group:"))
async def group_menu_action(callback: CallbackQuery):
    if callback.message is None or callback.message.chat.type not in {"group", "supergroup"}:
        await callback.answer("این منو فقط در گروه کلاس فعال است.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    pending = []
    class_name = school_name = ""
    answer = ""
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, callback.from_user, private=False)
            context = await get_group_context(session, callback.message.chat.id)
            if not context:
                await callback.answer("این گروه به یک کلاس فعال متصل نیست.", show_alert=True)
                return
            class_group, _, school = context
            class_name, school_name = class_group.name, school.name
            if action == "pending":
                if not await can_approve(session, user, class_group.id):
                    await callback.answer("مجوز مشاهده صف بررسی را نداری.", show_alert=True)
                    return
                pending = await list_pending_records(session, class_group.id)
            elif action == "homework":
                answer = _format_homework(await list_homework(session, class_group.id))
            elif action == "exams":
                answer = _format_exams(await list_exams(session, class_group.id), school.timezone)
            elif action == "week":
                local_date = datetime.now(ZoneInfo(school.timezone)).date()
                entries = (
                    await session.execute(
                        select(TimetableEntry, Subject)
                        .join(Subject, Subject.id == TimetableEntry.subject_id)
                        .where(
                            TimetableEntry.class_id == class_group.id,
                            TimetableEntry.deleted_at.is_(None),
                            Subject.deleted_at.is_(None),
                            or_(TimetableEntry.valid_from.is_(None), TimetableEntry.valid_from <= local_date),
                            or_(TimetableEntry.valid_until.is_(None), TimetableEntry.valid_until >= local_date),
                        )
                        .order_by(TimetableEntry.weekday, TimetableEntry.period)
                    )
                ).all()
                if not entries:
                    answer = "برنامه هفتگی معتبر هنوز برای این کلاس ثبت نشده است."
                else:
                    rows = ["📅 <b>برنامه هفتگی معتبر</b>"]
                    for entry, subject in entries[:35]:
                        start = f" ({entry.starts_at.strftime('%H:%M')})" if entry.starts_at else ""
                        rows.append(
                            f"• {WEEKDAYS[entry.weekday]} — زنگ {entry.period}{start}: "
                            f"{escape(subject.name)}"
                        )
                    answer = "\n".join(rows)
            elif action == "today":
                local_date = datetime.now(ZoneInfo(school.timezone)).date()
                reports = await list_reports(session, class_group.id, local_date)
                homework_rows = [
                    row for row in await list_homework(session, class_group.id)
                    if row[0].due_on == local_date
                ]
                exam_rows = [
                    row for row in await list_exams(session, class_group.id)
                    if row[0].scheduled_at.astimezone(ZoneInfo(school.timezone)).date() == local_date
                ]
                rows = [f"🗓 <b>خلاصه امروز — {jalali_date(local_date)}</b>"]
                if reports:
                    rows.append("🗒 <b>گزارش‌های تأییدشده</b>")
                    for report in reports[:3]:
                        title = escape((report.title or "گزارش روزانه")[:60])
                        body = escape((report.body or "")[:180])
                        if report.body and len(report.body) > 180:
                            body += "…"
                        rows.append(f"• <b>{title}</b>\n{body}")
                    if len(reports) > 3:
                        rows.append(f"و {len(reports) - 3} گزارش دیگر.")
                else:
                    rows.append("🗒 گزارش تأییدشده‌ای برای امروز ثبت نشده است.")
                if homework_rows:
                    rows.append("📚 <b>تکالیف موعددار امروز</b>")
                    rows.append(_format_homework(homework_rows[:3]))
                else:
                    rows.append("📚 امروز تکلیف موعدداری ثبت نشده است.")
                if exam_rows:
                    rows.append("📝 <b>امتحانات امروز</b>")
                    rows.append(_format_exams(exam_rows[:3], school.timezone))
                else:
                    rows.append("📝 امروز امتحانی ثبت نشده است.")
                answer = "\n\n".join(rows)
                if len(answer.encode("utf-16-le")) // 2 > 3900:
                    answer = answer[:3500] + "\n\n<i>خلاصه برای محدودیت طول پیام کوتاه شد.</i>"
            else:
                await callback.answer("این گزینه شناخته‌شده نیست.", show_alert=True)
                return

    if action == "pending":
        if not pending:
            await callback.message.answer("🕓 موردی در انتظار بررسی نیست.")
        else:
            for kind, record in pending[:8]:
                title = getattr(record, "title", "گزارش روزانه")
                details = getattr(record, "description", getattr(record, "body", getattr(record, "scope", "")))
                await callback.message.answer(
                    f"🕓 <b>{escape(ENTITY_LABELS.get(kind, 'اطلاعات'))} #{record.id} — "
                    f"{escape(title)}</b>\n{escape(details[:1000])}",
                    reply_markup=approval_keyboard(kind, record.id),
                )
            if len(pending) > 8:
                await callback.message.answer("فقط ۸ مورد نخست صف بررسی نمایش داده شد.")
    else:
        await callback.message.answer(
            f"🏫 <b>{escape(class_name)} — {escape(school_name)}</b>\n\n{answer}"
        )
    await callback.answer()
