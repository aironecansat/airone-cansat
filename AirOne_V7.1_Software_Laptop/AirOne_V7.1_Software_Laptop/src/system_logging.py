"""Structured logging: JSON rotating file handler + coloured console handler.

Call :func:`setup_logging` once at startup. A per-thread correlation id can be
attached via :class:`LogContext` and is emitted with every record.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import threading
from datetime import datetime, timezone
from typing import Optional

_context = threading.local()

_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime"}


class LogContext:
    """Per-thread correlation id carried into every log record."""

    @staticmethod
    def set_request_id(request_id: Optional[str]) -> None:
        _context.request_id = request_id

    @staticmethod
    def get_request_id() -> Optional[str]:
        return getattr(_context, "request_id", None)

    @staticmethod
    def clear() -> None:
        _context.request_id = None


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = LogContext.get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        rid = getattr(record, "request_id", None)
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Include any user-supplied extra fields.
        for k, v in record.__dict__.items():
            if k not in _RESERVED and k not in payload and k != "request_id":
                try:
                    json.dumps(v)
                    payload[k] = v
                except (TypeError, ValueError):
                    payload[k] = str(v)
        return json.dumps(payload, separators=(",", ":"))


_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[41m\033[97m",
}
_RESET = "\033[0m"


class ColoredConsoleFormatter(logging.Formatter):
    def __init__(self, use_color: bool = True) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        self.use_color = use_color and os.environ.get("NO_COLOR") is None

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self.use_color:
            color = _COLORS.get(record.levelname, "")
            return f"{color}{text}{_RESET}"
        return text


def setup_logging(
    level: str = "INFO",
    log_dir: str = "logs",
    json_output: bool = True,
    console: bool = True,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 10,
) -> logging.Logger:
    """Configure the root logger. Idempotent within a process run."""

    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove pre-existing handlers so repeated calls don't duplicate output.
    for h in list(root.handlers):
        root.removeHandler(h)

    ctx_filter = _ContextFilter()

    if json_output:
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "airone.jsonl"),
            maxBytes=max_bytes, backupCount=backup_count,
        )
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(ctx_filter)
        root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(ColoredConsoleFormatter())
        console_handler.addFilter(ctx_filter)
        root.addHandler(console_handler)

    logging.getLogger(__name__).info("Logging initialised (level=%s)", level)
    return root
