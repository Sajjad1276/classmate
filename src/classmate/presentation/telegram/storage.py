import json
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram.fsm.storage.base import BaseStorage, DataType, StateType, StorageKey
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from classmate.db.models import BotState
from classmate.db.session import SessionFactory


class DatabaseFSMStorage(BaseStorage):
    """Persistent aiogram FSM storage backed by PostgreSQL.

    Wizard state survives bot restarts. Only JSON-compatible values should be
    stored in FSM data; V2 stores primitive values and database IDs only.
    """

    def __init__(self, ttl: timedelta = timedelta(days=7)) -> None:
        self._ttl = ttl

    @staticmethod
    def _key(key: StorageKey) -> str:
        payload = {
            "bot": key.bot_id,
            "chat": key.chat_id,
            "user": key.user_id,
            "thread": key.thread_id,
            "business": key.business_connection_id,
            "destiny": key.destiny,
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def _expiry(self) -> datetime:
        return datetime.now(timezone.utc) + self._ttl

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_value = state.state if hasattr(state, "state") else state
        statement = pg_insert(BotState).values(
            key=self._key(key),
            state=state_value,
            data={},
            expires_at=self._expiry(),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[BotState.key],
            set_={"state": state_value, "expires_at": self._expiry()},
        )
        async with SessionFactory() as session:
            async with session.begin():
                await session.execute(statement)

    async def get_state(self, key: StorageKey) -> str | None:
        now = datetime.now(timezone.utc)
        async with SessionFactory() as session:
            async with session.begin():
                row = await session.get(BotState, self._key(key))
                if row is None:
                    return None
                if row.expires_at <= now:
                    await session.delete(row)
                    return None
                return row.state

    async def set_data(self, key: StorageKey, data: DataType) -> None:
        safe_data: dict[str, Any] = dict(data)
        statement = pg_insert(BotState).values(
            key=self._key(key),
            state=None,
            data=safe_data,
            expires_at=self._expiry(),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[BotState.key],
            set_={"data": safe_data, "expires_at": self._expiry()},
        )
        async with SessionFactory() as session:
            async with session.begin():
                await session.execute(statement)

    async def get_data(self, key: StorageKey) -> DataType:
        now = datetime.now(timezone.utc)
        async with SessionFactory() as session:
            async with session.begin():
                row = await session.get(BotState, self._key(key))
                if row is None:
                    return {}
                if row.expires_at <= now:
                    await session.delete(row)
                    return {}
                return dict(row.data or {})

    async def update_data(self, key: StorageKey, data: DataType) -> DataType:
        current = await self.get_data(key)
        current.update(data)
        await self.set_data(key, current)
        return current

    async def clear(self, key: StorageKey) -> None:
        async with SessionFactory() as session:
            async with session.begin():
                await session.execute(
                    delete(BotState).where(BotState.key == self._key(key))
                )

    async def close(self) -> None:
        # SessionFactory owns the shared engine; the bot lifespan disposes it.
        return None
