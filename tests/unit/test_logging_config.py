"""Unit tests for logging_config.py: text and JSON handlers."""

from __future__ import annotations

import json
import logging

from newsdrop.logging_config import _JsonFormatter, setup_logging


def _newsdrop_handlers(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if getattr(h, "name", None) == "newsdrop"]


def test_setup_logging_text_format_installs_single_handler():
    root = logging.getLogger()
    original_level = root.level
    try:
        setup_logging()
        handlers = _newsdrop_handlers(root)
        assert len(handlers) == 1
        assert root.level == logging.INFO
    finally:
        for handler in _newsdrop_handlers(root):
            root.removeHandler(handler)
        root.setLevel(original_level)


def test_setup_logging_replaces_previous_newsdrop_handler():
    root = logging.getLogger()
    original_level = root.level
    stale = logging.StreamHandler()
    stale.set_name("newsdrop")
    root.addHandler(stale)
    try:
        setup_logging()
        assert len(_newsdrop_handlers(root)) == 1
        assert stale not in _newsdrop_handlers(root)
    finally:
        for handler in _newsdrop_handlers(root):
            root.removeHandler(handler)
        root.setLevel(original_level)


def test_json_formatter_emits_parseable_record():
    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name="newsdrop.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "newsdrop.test"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload
    assert "exception" not in payload


def test_json_formatter_includes_exception_info():
    formatter = _JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="newsdrop.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=None,
            exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert "ValueError" in payload["exception"]
    assert payload["message"] == "failed"


def test_setup_logging_json_format(monkeypatch):
    monkeypatch.setattr("newsdrop.logging_config.LOG_FORMAT", "json")
    monkeypatch.setattr("newsdrop.logging_config.LOG_LEVEL", "DEBUG")
    root = logging.getLogger()
    original_level = root.level
    try:
        setup_logging()
        handlers = _newsdrop_handlers(root)
        assert len(handlers) == 1
        assert isinstance(handlers[0].formatter, _JsonFormatter)
        assert root.level == logging.DEBUG
    finally:
        for handler in _newsdrop_handlers(root):
            root.removeHandler(handler)
        root.setLevel(original_level)
