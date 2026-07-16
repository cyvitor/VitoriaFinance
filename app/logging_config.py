import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import get_settings


LOGGER_NAME = "vitoria.bot"


def configure_bot_logging() -> logging.Logger:
    settings = get_settings()
    logger = logging.getLogger(LOGGER_NAME)
    if getattr(logger, "_vitoria_configured", False):
        return logger
    log_dir = Path(settings.bot_log_dir)
    if not log_dir.is_absolute():
        log_dir = Path.cwd() / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if settings.bot_log_level == "detailed" else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S%z"
    )
    file_handler = RotatingFileHandler(
        log_dir / "vitoria-bot.log",
        maxBytes=max(settings.bot_log_max_bytes, 1024),
        backupCount=max(settings.bot_log_backup_count, 1),
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.setLevel(level)
    logger.propagate = False
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger._vitoria_configured = True
    logger.info("logging_configured level=%s directory=%s", settings.bot_log_level, log_dir)
    if settings.bot_log_level == "detailed":
        logger.warning("detailed_logging_enabled conversational_financial_data_will_be_recorded")
    return logger


def get_bot_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
