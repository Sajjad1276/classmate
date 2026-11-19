from __future__ import annotations

from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StorageKey
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from classmate.db.models import FSMStateRecord


class PostgresFSMStorage(BaseStorage):
    """Persist aiogram FSM state and data in PostgreSQL across restarts/deploys."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._key_builder = DefaultKeyBuilder(
            prefix="classmate-fsm",
            with_bot_id=True,
            with_business_connection_id=True,
            with_destiny=True,
        )

    def _key(self, key: StorageKey) -> str:
        return self._key_builder.build(key)

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(FSMStateRecord.state).where(FSMStateRecord.key == self._key(key))
            )

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        value = state.state if isinstance(state, State) else state
        storage_key = self._key(key)
        statement = insert(FSMStateRecord).values(key=storage_key, state=value, data={})
        statement = statement.on_conflict_do_update(
            index_elements=[FSMStateRecord.key],
            set_={"state": value},
        )
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(statement)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._session_factory() as session:
            data = await session.scalar(
                select(FSMStateRecord.data).where(FSMStateRecord.key == self._key(key))
            )
        return dict(data or {})

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        storage_key = self._key(key)
        statement = insert(FSMStateRecord).values(key=storage_key, state=None, data=data)
        statement = statement.on_conflict_do_update(
            index_elements=[FSMStateRecord.key],
            set_={"data": data},
        )
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(statement)

    async def close(self) -> None:
        # Engine lifecycle is owned by db.session and the application shutdown hook.
        return None
