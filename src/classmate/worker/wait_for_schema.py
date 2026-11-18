from __future__ import annotations

import asyncio
import logging
import time

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from classmate.core.config import get_settings

logger = logging.getLogger(__name__)

POLL_SECONDS = 5
MAX_WAIT_SECONDS = 600


def schema_is_current(applied_revisions: set[str], head_revisions: set[str]) -> bool:
    """Return True only when every repository migration head is applied."""
    return bool(head_revisions) and head_revisions.issubset(applied_revisions)


def migration_heads() -> set[str]:
    config = Config("alembic.ini")
    return set(ScriptDirectory.from_config(config).get_heads())


async def wait_for_schema() -> None:
    settings = get_settings()
    heads = migration_heads()
    engine = create_async_engine(settings.async_database_url, pool_pre_ping=True)
    deadline = time.monotonic() + MAX_WAIT_SECONDS

    try:
        while time.monotonic() < deadline:
            try:
                async with engine.connect() as connection:
                    result = await connection.execute(text("SELECT version_num FROM alembic_version"))
                    applied = set(result.scalars().all())
                if schema_is_current(applied, heads):
                    logger.info("Database schema is current at revisions: %s", sorted(heads))
                    return
                logger.info(
                    "Waiting for database migrations; applied=%s expected=%s",
                    sorted(applied),
                    sorted(heads),
                )
            except SQLAlchemyError as exc:
                logger.info("Database schema is not ready yet: %s", exc)

            await asyncio.sleep(POLL_SECONDS)
    finally:
        await engine.dispose()

    raise TimeoutError(
        f"Database migrations did not reach {sorted(heads)} within {MAX_WAIT_SECONDS} seconds"
    )


def main() -> None:
    logging.basicConfig(level=settings_log_level())
    asyncio.run(wait_for_schema())


def settings_log_level() -> str:
    return get_settings().log_level


if __name__ == "__main__":
    main()
