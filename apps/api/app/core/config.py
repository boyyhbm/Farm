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
    deployment_mode: str = "server"
    desktop_data_dir: str = ""
    agent_installer_path: str = ""
    desktop_network_origin: str = ""


@lru_cache
def settings():
    return Settings()


def allowed_origin(origin):
    config = settings()
    return origin == config.public_origin or (
        config.deployment_mode == "desktop"
        and bool(config.desktop_network_origin)
        and origin == config.desktop_network_origin
    )
