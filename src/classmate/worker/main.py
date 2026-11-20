import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from sqlalchemy import select

from classmate.core.config import get_settings
from classmate.core.logging import configure_logging
from classmate.db.models import DailyReport, Exam, Homework, ReminderJob
from classmate.domain.enums import ReminderStatus
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
                        ReminderJob.status == ReminderStatus.PROCESSING,
                        ReminderJob.locked_at < cutoff,
                    )
                )
            ).scalars().all()
            for item in stale:
                # The process may have crashed after Telegram accepted the message but
                # before the DB commit. Retrying here can create a duplicate, so require
                # an explicit operator decision for ambiguous sends.
                item.status = ReminderStatus.UNCERTAIN
                item.last_error = "Delivery outcome unknown after worker interruption; manual review required."
                item.locked_at = None

            job = await session.scalar(
                select(ReminderJob)
                .where(
                    ReminderJob.status.in_([ReminderStatus.PENDING, ReminderStatus.RETRY]),
                    ReminderJob.scheduled_at <= datetime.now(timezone.utc),
                )
                .order_by(ReminderJob.scheduled_at, ReminderJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.status = ReminderStatus.PROCESSING
            job.locked_at = datetime.now(timezone.utc)
            job.attempts += 1
            await session.flush()
            return ReminderJobSnapshot(job.id, job.target_id, job.message_text, job.attempts)


async def job_is_still_sendable(job_id: int) -> bool:
    """Re-check cancellation and source-record status immediately before Telegram send."""
    async with SessionFactory() as session:
        async with session.begin():
            job = await session.get(ReminderJob, job_id, with_for_update=True)
            if job is None or job.status != ReminderStatus.PROCESSING:
                return False
            model = {
                "homework": Homework,
                "exam": Exam,
                "report": DailyReport,
            }.get(job.entity_type)
            if model is None:
                job.status = ReminderStatus.CANCELLED
                job.locked_at = None
                job.last_error = "Reminder source type is no longer supported."
                return False
            record = await session.get(model, job.entity_id)
            if record is None or record.status != "APPROVED":
                job.status = ReminderStatus.CANCELLED
                job.locked_at = None
                job.last_error = "Source record is missing or no longer approved."
                return False
            return True


async def finish_job(
    job_id: int,
    error: str | None = None,
    attempts: int = 1,
    *,
    retry_after_seconds: int | None = None,
    uncertain: bool = False,
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            job = await session.get(ReminderJob, job_id, with_for_update=True)
            if job is None:
                return
            if job.status == ReminderStatus.CANCELLED:
                # A source record can be cancelled while the worker is between claim and send.
                return
            job.locked_at = None
            if error is None:
                job.status = ReminderStatus.SENT
                job.sent_at = datetime.now(timezone.utc)
                job.last_error = None
                return
            job.last_error = error[:2000]
            if uncertain:
                job.status = ReminderStatus.UNCERTAIN
                return
            if attempts >= MAX_ATTEMPTS:
                job.status = ReminderStatus.FAILED
            else:
                job.status = ReminderStatus.RETRY
                delay_seconds = (
                    max(1, retry_after_seconds)
                    if retry_after_seconds is not None
                    else min(3600, 30 * (2 ** max(0, attempts - 1)))
                )
                job.scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)


async def run_worker() -> None:
    configure_logging()
    settings = get_settings()
    token = settings.bot_token.strip()
    if not token or token.startswith("SET_ME__"):
        raise RuntimeError(
            "BOT_TOKEN is not configured. Replace the Railway placeholder with a token from @BotFather."
        )
    await wait_for_schema()
    bot = Bot(
        token=token,
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
                if not await job_is_still_sendable(job.id):
                    logger.info("Reminder job %s was cancelled before delivery", job.id)
                    continue
                await bot.send_message(job.target_id, job.message_text)
                await finish_job(job.id)
                logger.info("Reminder job %s delivered", job.id)
            except TelegramRetryAfter as exc:
                logger.warning("Reminder job %s rate-limited: %s", job.id, exc)
                await finish_job(
                    job.id, str(exc), job.attempts,
                    retry_after_seconds=int(exc.retry_after),
                )
            except (TelegramNetworkError, TelegramServerError) as exc:
                logger.warning("Reminder job %s has an ambiguous delivery result: %s", job.id, exc)
                await finish_job(job.id, str(exc), job.attempts, uncertain=True)
            except TelegramAPIError as exc:
                logger.warning("Reminder job %s rejected by Telegram: %s", job.id, exc)
                await finish_job(job.id, str(exc), MAX_ATTEMPTS)
            except Exception as exc:
                logger.exception("Unexpected error sending reminder job %s", job.id)
                await finish_job(job.id, str(exc), job.attempts, uncertain=True)
    finally:
        await bot.session.close()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run_worker())
