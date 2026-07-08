"""Application logging configuration.

Supports plain text (default) and structured JSON output. Set
``LOG_FORMAT=json`` for production environments where logs are collected
by a centralized logging system.
"""

from __future__ import annotations

import json
import logging
import os
import sys

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "text").lower()


class _JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging() -> None:
    """Configure root logger for the application."""
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name("newsdrop")

    if LOG_FORMAT == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    # Replace existing handlers to avoid duplicate logs on reload.
    for existing in list(root.handlers):
        if getattr(existing, "set_name", None) and existing.name == "newsdrop":
            root.removeHandler(existing)
    root.addHandler(handler)

    # Avoid logging full request URLs (Telegram bot tokens appear in httpx INFO lines).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
