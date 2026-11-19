import asyncio
import logging
from dataclasses import dataclass
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
LEASE_DURATION = timedelta(minutes=5)
POLL_SECONDS_MIN = 1
POLL_SECONDS_MAX = 30


@dataclass(frozen=True, slots=True)
class ReminderLease:
    job_id: int
    target_id: int
    message_text: str
    attempt: int
    token: datetime


async def claim_due_job() -> ReminderLease | None:
    """Recover expired leases and claim one due job using PostgreSQL SKIP LOCKED."""
    now = datetime.now(timezone.utc)
    async with SessionFactory() as session:
        async with session.begin():
            stale = list(
                (
                    await session.scalars(
                        select(ReminderJob)
                        .where(
                            ReminderJob.status == "processing",
                            ReminderJob.locked_at < now - LEASE_DURATION,
                        )
                        .order_by(ReminderJob.locked_at, ReminderJob.id)
                        .with_for_update(skip_locked=True)
                        .limit(100)
                    )
                ).all()
            )
            for item in stale:
                item.locked_at = None
                if item.attempts >= MAX_ATTEMPTS:
                    item.status = "failed"
                    item.last_error = "Retry limit reached after an expired lease."
                else:
                    item.status = "retry"
                    item.scheduled_at = now
                    item.last_error = "Recovered after an interrupted worker lease."

            job = await session.scalar(
                select(ReminderJob)
                .where(
                    ReminderJob.status.in_({"pending", "retry"}),
                    ReminderJob.scheduled_at <= now,
                )
                .order_by(ReminderJob.scheduled_at, ReminderJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            token = now
            job.status = "processing"
            job.locked_at = token
            job.attempts += 1
            await session.flush()
            return ReminderLease(
                job_id=job.id,
                target_id=job.target_id,
                message_text=job.message_text,
                attempt=job.attempts,
                token=token,
            )


async def lease_is_current(lease: ReminderLease) -> bool:
    async with SessionFactory() as session:
        async with session.begin():
            job = await session.get(ReminderJob, lease.job_id)
            return bool(
                job
                and job.status == "processing"
                and job.locked_at == lease.token
            )


async def finish_job(
    lease: ReminderLease,
    *,
    error: str | None = None,
) -> bool:
    """Finish only the exact lease that performed the external send."""
    async with SessionFactory() as session:
        async with session.begin():
            job = await session.get(ReminderJob, lease.job_id, with_for_update=True)
            if (
                job is None
                or job.status != "processing"
                or job.locked_at != lease.token
            ):
                logger.warning("Ignoring stale result for reminder job %s", lease.job_id)
                return False

            job.locked_at = None
            if error is None:
                job.status = "sent"
                job.sent_at = datetime.now(timezone.utc)
                job.last_error = None
                return True

            job.last_error = error[:2000]
            if lease.attempt >= MAX_ATTEMPTS:
                job.status = "failed"
            else:
                job.status = "retry"
                delay = min(3600, 30 * (2 ** max(0, lease.attempt - 1)))
                job.scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            return True


async def run_worker() -> None:
    configure_logging()
    settings = get_settings()
    token = settings.bot_token.strip()
    if not token or token.startswith("SET_ME__"):
        raise RuntimeError("BOT_TOKEN is not configured.")

    await wait_for_schema()
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    idle_seconds = max(POLL_SECONDS_MIN, min(settings.reminder_poll_seconds, POLL_SECONDS_MAX))
    logger.info("ClassMate V2 reminder worker started.")
    try:
        while True:
            lease = await claim_due_job()
            if lease is None:
                await asyncio.sleep(idle_seconds)
                continue
            if not await lease_is_current(lease):
                logger.info("Skipping cancelled/superseded reminder %s", lease.job_id)
                continue
            try:
                await bot.send_message(lease.target_id, lease.message_text)
            except TelegramAPIError as exc:
                logger.warning("Reminder %s delivery failed: %s", lease.job_id, exc)
                await finish_job(lease, error=str(exc))
            except Exception as exc:
                logger.exception("Unexpected failure for reminder %s", lease.job_id)
                await finish_job(lease, error=str(exc))
            else:
                await finish_job(lease)
                logger.info("Reminder %s delivered.", lease.job_id)
    finally:
        await bot.session.close()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run_worker())
