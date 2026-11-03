from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./farm.db"
    redis_url: str = "redis://localhost:6379/0"
    public_origin: str = "http://localhost:3000"
    session_secure: bool = True
    admin_username: str = "admin"
    admin_password: str = ""
    session_hours: int = 12
    node_offline_seconds: int = 45
    retention_days: int = 30
    scheduler_max_parallel: int = 10
    allow_test_sqlite: bool = False


@lru_cache
def settings():
    return Settings()
