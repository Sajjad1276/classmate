from datetime import datetime, timezone
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from classmate.bot.handlers import _user, _ensure_group_member
from classmate.bot.keyboards import main_menu
from classmate.db.models import ClassGroup
from classmate.db.session import SessionFactory
from classmate.services.permissions import hash_code

router = Router(name="classmate_private")


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
