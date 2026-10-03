"""Logging: console + rotating file, timestamped plain text lines."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from time import strftime

from . import APP_SLUG
from .config import config_dir

_LOGGER_NAME = APP_SLUG


def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", "%H:%M:%S")
    _ts = strftime("%Y-%m-%d_%H%M%S")

    # file handler
    log_dir = config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        log_dir / f"{_ts}.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # console handler
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)
