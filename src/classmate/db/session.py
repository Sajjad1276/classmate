from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from classmate.core.config import get_settings

settings = get_settings()
engine = create_async_engine(
    settings.async_database_url,
    pool_pre_ping=True,
    echo=False,
)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def dispose_engine() -> None:
    await engine.dispose()
