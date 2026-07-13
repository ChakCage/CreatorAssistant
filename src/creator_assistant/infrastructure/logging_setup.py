import logging
from logging.handlers import RotatingFileHandler

from .settings_store import local_data_root


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("creator_assistant")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    log_dir = local_data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "creator_assistant.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    return logger
