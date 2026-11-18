import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from classmate.core.config import get_settings
from classmate.core.logging import configure_logging
from classmate.db.models import ReminderJob
from classmate.db.session import SessionFactory, dispose_engine
from classmate.worker.wait_for_schema import wait_for_schema

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 6
LOCK_TIMEOUT = timedelta(minutes=10)


class ReminderJobSnapshot:
    def __init__(self, id: int, target_id: int, message_text: str, attempts: int):
        self.id = id
        self.target_id = target_id
        self.message_text = message_text
        self.attempts = attempts


async def claim_due_job() -> ReminderJobSnapshot | None:
    """Claim one due job with SKIP LOCKED so multiple workers do not claim the same row."""
    async with SessionFactory() as session:
        async with session.begin():
            cutoff = datetime.now(timezone.utc) - LOCK_TIMEOUT
            stale = (
                await session.execute(
                    select(ReminderJob).where(
                        ReminderJob.status == "processing",
                        ReminderJob.locked_at < cutoff,
                    )
                )
            ).scalars().all()
            for item in stale:
                item.status = "retry"
                item.scheduled_at = datetime.now(timezone.utc)
                item.last_error = "Recovered after an interrupted worker lease."
                item.locked_at = None

            job = await session.scalar(
                select(ReminderJob)
                .where(
                    ReminderJob.status.in_(["pending", "retry"]),
                    ReminderJob.scheduled_at <= datetime.now(timezone.utc),
                )
                .order_by(ReminderJob.scheduled_at, ReminderJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.status = "processing"
            job.locked_at = datetime.now(timezone.utc)
            job.attempts += 1
            await session.flush()
            return ReminderJobSnapshot(job.id, job.target_id, job.message_text, job.attempts)


async def finish_job(job_id: int, error: str | None = None, attempts: int = 1) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            job = await session.get(ReminderJob, job_id, with_for_update=True)
            if job is None:
                return
            job.locked_at = None
            if error is None:
                job.status = "sent"
                job.sent_at = datetime.now(timezone.utc)
                job.last_error = None
                return
            job.last_error = error[:2000]
            if attempts >= MAX_ATTEMPTS:
                job.status = "failed"
            else:
                job.status = "retry"
                delay_seconds = min(3600, 30 * (2 ** max(0, attempts - 1)))
                job.scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)


async def run_worker() -> None:
    configure_logging()
    settings = get_settings()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is missing.")
    await wait_for_schema()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    logger.info("ClassMate reminder worker started")
    try:
        while True:
            job = await claim_due_job()
            if job is None:
                await asyncio.sleep(max(1, settings.reminder_poll_seconds))
                continue
            try:
                await bot.send_message(job.target_id, job.message_text)
                await finish_job(job.id)
                logger.info("Reminder job %s delivered", job.id)
            except TelegramAPIError as exc:
                logger.warning("Reminder job %s failed: %s", job.id, exc)
                await finish_job(job.id, str(exc), job.attempts)
            except Exception as exc:
                logger.exception("Unexpected error sending reminder job %s", job.id)
                await finish_job(job.id, str(exc), job.attempts)
    finally:
        await bot.session.close()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run_worker())
