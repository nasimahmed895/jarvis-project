"""
core/logging_setup.py — structured JSON logging for JARVIS.

Stdlib only. One-call init, idempotent. Attaches a ContextVar-backed trace_id
to every record so tool calls can be correlated across worker / planner /
tool-dispatch boundaries. Emits to stderr (human-tailable) and a rotating
file in logs/jarvis.log.

Usage:

    from core.logging_setup import init_logging, get_logger, with_trace_id, new_trace_id

    init_logging()                           # once at process start
    log = get_logger(__name__)
    log.info("startup", extra={"phase": "boot"})

    with with_trace_id(new_trace_id()):
        log.info("tool_call", extra={"tool": "browser_control"})

Modules keep their existing `print()` for user-facing UI lines. The logger is
additive — add `log.*` calls where structured context helps (tool dispatch,
worker steps, error paths).
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

_LOG_DIR     = Path(__file__).resolve().parent.parent / "logs"
_LOG_FILE    = _LOG_DIR / "jarvis.log"
_ROTATE_MB   = 10
_BACKUPS     = 5

_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

_initialized = False


def new_trace_id() -> str:
    """12-hex-char trace id — short enough for log skimming, wide enough for uniqueness."""
    return secrets.token_hex(6)


@contextmanager
def with_trace_id(trace_id: str) -> Iterator[str]:
    """Scope a trace id to a block; nested calls stack cleanly."""
    token = _trace_id_var.set(trace_id)
    try:
        yield trace_id
    finally:
        _trace_id_var.reset(token)


def current_trace_id() -> str:
    return _trace_id_var.get()


class _JsonFormatter(logging.Formatter):
    """Serialize every record as a single-line JSON object."""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts":       time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level":    record.levelname,
            "logger":   record.name,
            "msg":      record.getMessage(),
            "trace_id": _trace_id_var.get() or None,
        }
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def init_logging(level: int = logging.INFO, log_path: Optional[Path] = None) -> None:
    """Set up root logger exactly once. Safe to call from any entry point."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    path = log_path or _LOG_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        path = None

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = _JsonFormatter()

    stderr_h = logging.StreamHandler()
    stderr_h.setFormatter(fmt)
    root.addHandler(stderr_h)

    if path is not None:
        try:
            file_h = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=_ROTATE_MB * 1024 * 1024,
                backupCount=_BACKUPS,
                encoding="utf-8",
            )
            file_h.setFormatter(fmt)
            root.addHandler(file_h)
        except OSError:
            pass

    # Silence overly chatty third-party libs
    for noisy in ("urllib3", "asyncio", "websockets.client", "websockets.server"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger. init_logging() is lazy-invoked if the caller forgot."""
    if not _initialized:
        try:
            init_logging()
        except Exception:
            pass
    return logging.getLogger(name)


# Honor an env override so operators can bump verbosity without a code change
_env_level = os.environ.get("JARVIS_LOG_LEVEL", "").upper()
if _env_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    init_logging(level=getattr(logging, _env_level))
