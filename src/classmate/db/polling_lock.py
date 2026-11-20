from __future__ import annotations

import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = logging.getLogger(__name__)

# Stable, application-specific PostgreSQL advisory lock key (signed 64-bit integer).
POLLING_LOCK_KEY = 0x434C4153534D4154


async def acquire_polling_lock(engine: AsyncEngine) -> AsyncConnection:
    """Wait until this process exclusively owns Telegram long polling.

    The lock is session-scoped, so the returned connection must remain open for
    the entire polling lifetime. Commit after acquisition to avoid holding an
    idle transaction while preserving the advisory lock on the connection.
    """
    connection = await engine.connect()
    try:
        await connection.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": POLLING_LOCK_KEY},
        )
        await connection.commit()
        logger.info("Acquired the ClassMate Telegram polling lock")
        return connection
    except BaseException:
        await connection.close()
        raise


async def release_polling_lock(connection: AsyncConnection) -> None:
    """Release the polling lock on the exact PostgreSQL session that acquired it."""
    try:
        result = await connection.execute(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": POLLING_LOCK_KEY},
        )
        unlocked = bool(result.scalar_one())
        await connection.commit()
        if unlocked:
            logger.info("Released the ClassMate Telegram polling lock")
        else:
            logger.warning("ClassMate polling lock was not held by this connection")
    finally:
        await connection.close()
