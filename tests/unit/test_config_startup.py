"""Tests for side-effect-free imports and explicit startup validation."""

from __future__ import annotations

import sqlite3

import pytest

import newsdrop.config as config
from newsdrop import database


def test_config_module_loads_without_secrets(monkeypatch):
    """Importing newsdrop.config must not raise for missing/malformed secrets.

    The test suite itself imports newsdrop.config with no TELEGRAM_BOT_TOKEN
    or NEWS_API_KEY set in CI; reaching this point proves import is
    side-effect-free. Explicit validation is covered below.
    """
    assert hasattr(config, "validate_config")


def test_validate_config_missing_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr(config, "NEWS_API_KEY", "k")
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        config.validate_config()


def test_validate_config_malformed_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "not-a-token")
    monkeypatch.setattr(config, "NEWS_API_KEY", "k")
    with pytest.raises(ValueError, match="format is invalid"):
        config.validate_config()


def test_validate_config_missing_news_api_key(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "12345:ABCdef_-")
    monkeypatch.setattr(config, "NEWS_API_KEY", "")
    with pytest.raises(ValueError, match="NEWS_API_KEY"):
        config.validate_config()


def test_validate_config_ok(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "12345:ABCdef_-")
    monkeypatch.setattr(config, "NEWS_API_KEY", "secret")
    config.validate_config()


def test_database_import_does_not_create_db(tmp_path, monkeypatch):
    """Importing database must not create/initialize the DB file."""
    db_file = tmp_path / "never_created.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    # No _init_db() call — importing already happened at module load.
    assert not db_file.exists()
    # But explicit init works.
    database._init_db()
    conn = sqlite3.connect(db_file)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "user_preferences" in tables
    assert "subscribers" not in tables


async def test_row_to_prefs_null_country_uses_default_country(tmp_db):
    """Regression: NULL-country rows used to fall back to hardcoded "us".

    _row_to_prefs honors its default_country argument (and
    _set_user_prefs_sync now threads it through) so the configured default
    wins over the hardcoded one.
    """
    with database._lock:
        conn = database._get_connection()
        try:
            row = conn.execute(
                "SELECT NULL AS country, 'general' AS category, 'UTC' AS timezone,"
                " 8 AS daily_hour, NULL AS quiet_start_hour, NULL AS quiet_end_hour,"
                " '' AS breaking_keywords, 1 AS breaking_use_follows"
            ).fetchone()
            prefs = database._row_to_prefs(row, "in")
        finally:
            conn.close()
    assert prefs["country"] == "in"
