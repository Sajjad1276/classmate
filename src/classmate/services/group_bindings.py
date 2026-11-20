from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.db.models import ClassGroup, School

# One transaction-scoped lock serializes binding changes across both tenant tables.
GROUP_BINDING_LOCK_KEY = 0x434C4153534D4154


async def lock_group_binding_mutations(session: AsyncSession) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": GROUP_BINDING_LOCK_KEY},
    )


async def is_chat_bound_to_class_group(session: AsyncSession, chat_id: int) -> bool:
    return (
        await session.scalar(
            select(ClassGroup.id).where(
                ClassGroup.group_chat_id == chat_id,
                ClassGroup.is_active.is_(True),
            )
        )
    ) is not None


async def is_chat_bound_to_school_group(session: AsyncSession, chat_id: int) -> bool:
    return (
        await session.scalar(
            select(School.id).where(
                School.telegram_group_id == chat_id,
                School.is_active.is_(True),
            )
        )
    ) is not None
