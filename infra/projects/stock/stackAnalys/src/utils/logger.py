from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from src.utils.config import load_settings, resolve_log_root


def setup_logger(log_file: str = "daily_update.log"):
    settings = load_settings()
    log_root = resolve_log_root(settings)
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = Path(log_root) / log_file
    logger.remove()
    logger.add(sys.stdout, level="INFO", enqueue=True)
    logger.add(log_path, level="INFO", rotation="20 MB", retention="30 days", encoding="utf-8", enqueue=True)
    return logger
