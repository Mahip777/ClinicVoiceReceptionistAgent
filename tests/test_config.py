from clinic_voice.config import Settings


def test_render_postgres_url_uses_installed_psycopg3_driver():
    settings = Settings(database_url="postgresql://user:password@host/database")
    assert settings.database_url == "postgresql+psycopg://user:password@host/database"


def test_legacy_postgres_url_is_normalized_too():
    settings = Settings(database_url="postgres://user:password@host/database")
    assert settings.database_url == "postgresql+psycopg://user:password@host/database"


def test_explicit_driver_and_sqlite_urls_are_unchanged():
    psycopg = Settings(database_url="postgresql+psycopg://user:password@host/database")
    sqlite = Settings(database_url="sqlite:///./test.db")
    assert psycopg.database_url == "postgresql+psycopg://user:password@host/database"
    assert sqlite.database_url == "sqlite:///./test.db"
