import os
import time

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from aiogram.fsm.storage.base import StorageKey

from classmate.bot.middlewares import ProcessedUpdateMiddleware
from classmate.db.fsm_storage import PostgresFSMStorage
from classmate.db.models import FSMStateRecord, ProcessedUpdate


@pytest.mark.asyncio
async def test_postgres_update_middleware_deduplicates_completed_updates():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for PostgreSQL middleware integration tests")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    middleware = ProcessedUpdateMiddleware(factory)
    update_id = int(time.time() * 1000) + 100_000_000_000
    try:
        assert await middleware._claim(update_id)
        assert not await middleware._claim(update_id)
        await middleware._finish(update_id)
        assert not await middleware._claim(update_id)
    finally:
        async with factory() as session:
            async with session.begin():
                await session.execute(delete(ProcessedUpdate).where(ProcessedUpdate.update_id == update_id))
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_fsm_storage_persists_state_and_data_across_instances():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for PostgreSQL FSM integration tests")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key = StorageKey(
        bot_id=999999, chat_id=999999, user_id=999999,
        thread_id=None, business_connection_id=None, destiny="default",
    )
    storage_a = PostgresFSMStorage(factory)
    storage_b = PostgresFSMStorage(factory)
    try:
        await storage_a.set_state(key, "PanelInput:input")
        await storage_a.set_data(key, {"panel_form": "join", "attempts": 2})
        assert await storage_b.get_state(key) == "PanelInput:input"
        assert await storage_b.get_data(key) == {"panel_form": "join", "attempts": 2}
    finally:
        async with factory() as session:
            async with session.begin():
                await session.execute(delete(FSMStateRecord).where(FSMStateRecord.key == storage_a._key(key)))
        await engine.dispose()
