import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from classmate.bot.handlers import _user
from classmate.db.models import ClassGroup, School
from classmate.db.session import SessionFactory
from classmate.services.permissions import hash_code, is_school_admin

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
