from classmate.core.config import Settings


def test_bootstrap_admin_ids_are_parsed():
    settings = Settings(bot_token="test", bootstrap_admin_ids="12, 34,invalid")
    assert settings.bootstrap_admin_id_set == {12, 34}


def test_postgres_url_is_normalized():
    settings = Settings(bot_token="test", database_url="postgres://user:pass@host/db")
    assert settings.async_database_url.startswith("postgresql+asyncpg://")
