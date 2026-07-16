from functools import lru_cache
import os
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv()


class Settings:
    app_name = os.getenv("APP_NAME", "VitoriaFinance")
    app_env = os.getenv("APP_ENV", "development")
    debug = os.getenv("APP_DEBUG", "false").lower() == "true"
    secret_key = os.getenv("SECRET_KEY", "change-me")
    db_driver = os.getenv("DB_DRIVER", "mysql+pymysql")
    db_host = os.getenv("DB_HOST", "127.0.0.1")
    db_port = int(os.getenv("DB_PORT", "3306"))
    db_name = os.getenv("DB_NAME", "vitoria_finance")
    db_user = os.getenv("DB_USER", "vitoria")
    db_password = os.getenv("DB_PASSWORD", "vitoria")
    db_charset = os.getenv("DB_CHARSET", "utf8mb4")
    session_https_only = os.getenv("SESSION_HTTPS_ONLY", "false").lower() == "true"
    bot_log_dir = os.getenv("BOT_LOG_DIR", "logs")
    bot_log_level = os.getenv("BOT_LOG_LEVEL", "basic").lower()
    bot_log_max_bytes = int(os.getenv("BOT_LOG_MAX_BYTES", "10485760"))
    bot_log_backup_count = int(os.getenv("BOT_LOG_BACKUP_COUNT", "7"))

    @property
    def database_url(self) -> str:
        # DATABASE_URL existe apenas como escape para testes e ambientes gerenciados.
        override = os.getenv("DATABASE_URL")
        if override:
            return override
        user = quote_plus(self.db_user)
        password = quote_plus(self.db_password)
        return (
            f"{self.db_driver}://{user}:{password}@{self.db_host}:"
            f"{self.db_port}/{self.db_name}?charset={self.db_charset}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
