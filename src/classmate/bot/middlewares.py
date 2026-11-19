from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware
from sqlalchemy import or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from classmate.db.models import ProcessedUpdate
from classmate.domain.enums import UpdateStatus

logger = logging.getLogger(__name__)
UPDATE_LEASE = timedelta(minutes=5)


class ProcessedUpdateMiddleware(BaseMiddleware):
    """Database-backed duplicate-update guard with an expiring processing lease."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _claim(self, update_id: int) -> bool:
        now = datetime.now(timezone.utc)
        statement = insert(ProcessedUpdate).values(
            update_id=update_id,
            processed_at=now,
            status=UpdateStatus.PROCESSING,
            lease_expires_at=now + UPDATE_LEASE,
            attempts=1,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ProcessedUpdate.update_id],
            set_={
                "status": UpdateStatus.PROCESSING,
                "lease_expires_at": now + UPDATE_LEASE,
                "attempts": ProcessedUpdate.attempts + 1,
                "last_error": None,
            },
            where=or_(
                ProcessedUpdate.status == UpdateStatus.FAILED,
                ProcessedUpdate.lease_expires_at < now,
            ),
        ).returning(ProcessedUpdate.update_id)
        async with self._session_factory() as session:
            async with session.begin():
                claimed = await session.scalar(statement)
        return claimed is not None

    async def _finish(self, update_id: int, *, error: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        values = {
            "status": UpdateStatus.FAILED if error else UpdateStatus.PROCESSED,
            "lease_expires_at": None,
            "last_error": error[:2000] if error else None,
        }
        if error is None:
            values["processed_at"] = now
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(ProcessedUpdate)
                    .where(ProcessedUpdate.update_id == update_id)
                    .values(**values)
                )

    async def __call__(self, handler, event, data):
        update_id = getattr(event, "update_id", None)
        if update_id is None:
            return await handler(event, data)
        if not await self._claim(int(update_id)):
            logger.info("Skipping duplicate or currently leased Telegram update %s", update_id)
            return None
        try:
            result = await handler(event, data)
        except Exception as exc:
            await self._finish(int(update_id), error=f"{type(exc).__name__}: {exc}")
            raise
        await self._finish(int(update_id))
        return result
