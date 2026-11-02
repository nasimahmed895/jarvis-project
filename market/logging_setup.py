"""Namespaced logger that writes to logs/market.log without touching stdout.

Importing this module does not open the log file. The handler is attached on
the first call to `get_logger`.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_LOGGER_NAME = "market"
_configured = False


def _logs_dir() -> Path:
    base = Path(__file__).resolve().parent.parent
    d = base / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_logger(name: str | None = None, level: str = "INFO") -> logging.Logger:
    """Return the market-package logger, configured on first call.

    `name` yields a child logger (`market.<name>`) that inherits the handler
    and level. The root `market` logger does NOT propagate to the root logger,
    so JARVIS's existing stdout stream stays uncluttered.
    """
    global _configured
    root = logging.getLogger(_LOGGER_NAME)
    if not _configured:
        root.setLevel(getattr(logging, level, logging.INFO))
        root.propagate = False
        log_path = _logs_dir() / "market.log"
        handler = RotatingFileHandler(
            log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
        _configured = True
    return root if name is None else root.getChild(name)
