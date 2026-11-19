from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.db.models import User


async def ensure_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    full_name: str,
    username: str | None,
    private_chat_started: bool,
    bootstrap_admin_ids: set[int],
) -> User:
    statement = pg_insert(User).values(
        telegram_id=telegram_id,
        full_name=(full_name or "کاربر")[:160],
        username=username or None,
        private_chat_started=private_chat_started,
        is_platform_admin=telegram_id in bootstrap_admin_ids,
        is_active=True,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[User.telegram_id],
        set_={
            "full_name": statement.excluded.full_name,
            "username": statement.excluded.username,
            "private_chat_started": (
                User.private_chat_started | statement.excluded.private_chat_started
            ),
            "is_platform_admin": (
                User.is_platform_admin | statement.excluded.is_platform_admin
            ),
        },
    )
    await session.execute(statement)
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        raise RuntimeError("User upsert completed without a readable user row.")
    return user
