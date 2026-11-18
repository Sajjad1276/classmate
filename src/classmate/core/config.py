from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    bot_token: str = ""
    database_url: str = "postgresql+asyncpg://classmate:classmate@localhost:5432/classmate"
    bootstrap_admin_ids: str = ""
    default_timezone: str = "Asia/Tehran"
    log_level: str = "INFO"
    reminder_poll_seconds: int = 5

    @property
    def bootstrap_admin_id_set(self) -> set[int]:
        result: set[int] = set()
        for value in self.bootstrap_admin_ids.split(","):
            value = value.strip()
            if value.isdigit():
                result.add(int(value))
        return result

    @property
    def async_database_url(self) -> str:
        url = self.database_url.strip()
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
