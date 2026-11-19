from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from classmate.bot.handlers import _class_from_message, _user
from classmate.bot.keyboards import approval_keyboard
from classmate.db.models import ReminderJob
from classmate.db.session import SessionFactory
from classmate.domain.enums import ENTITY_LABELS, EntityType
from classmate.services.permissions import can_approve
from classmate.services.records import (
    approve_record,
    cancel_approved_record,
    get_group_context,
    list_pending_records,
    reject_record,
)

router = Router(name="classmate_approval")


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
