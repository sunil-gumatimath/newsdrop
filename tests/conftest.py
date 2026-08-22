# ruff: noqa: E402
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path

_PRE_IMPORT_DB = Path(tempfile.gettempdir()) / "newsdrop_conftest_init.db"
os.environ.setdefault("DATABASE_PATH", str(_PRE_IMPORT_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC_PATH = _PROJECT_ROOT / "src"
_SRC_PATH_STR = str(_SRC_PATH)
if _SRC_PATH_STR not in sys.path:
    sys.path.insert(0, _SRC_PATH_STR)

from unittest.mock import AsyncMock, MagicMock

import pytest

from newsdrop import database as _database_mod
from newsdrop import state as _state_mod


def _cleanup_pre_import_db() -> None:
    if _PRE_IMPORT_DB.exists():
        with contextlib.suppress(OSError):
            _PRE_IMPORT_DB.unlink()
    for ext in ("-wal", "-shm"):
        sidecar = Path(str(_PRE_IMPORT_DB) + ext)
        if sidecar.exists():
            with contextlib.suppress(OSError):
                sidecar.unlink()


_cleanup_pre_import_db()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    db_file_str = str(db_file)

    monkeypatch.setattr(_database_mod, "DB_PATH", Path(db_file_str))

    # Close any cached connection from the pre-import DB path so the new
    # path takes effect without stale closed-connection errors.
    _database_mod._close_cached_connections()
    _database_mod._init_db()

    yield db_file_str

    # Teardown: close the test connection to release the file lock.
    _database_mod._close_cached_connections()


@pytest.fixture(autouse=True)
def _isolate_rate_limit_state():
    _state_mod.reset_backend()
    yield
    _state_mod.reset_backend()


@pytest.fixture
def mock_httpx_client():
    """Provide an AsyncMock httpx.AsyncClient with common response defaults.

    Usage::

        async def test_something(mock_httpx_client):
            mock_httpx_client.get.return_value = mock_httpx_client.Response(
                status_code=200,
                text="<rss>...</rss>",
            )
    """
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.text = ""
    response.json.return_value = {}
    response.headers = {}
    response.raise_for_status = MagicMock()
    client.Response = MagicMock(return_value=response)
    client.get = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    client.request = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.fixture
def mock_telegram_update():
    """Provide a MagicMock representing a Telegram Update with message context.

    Sets up ``effective_message``, ``effective_chat``, and ``effective_user``
    with sensible defaults so tests can read attributes or call methods
    without additional setup.
    """
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_message.message_id = 1
    update.effective_message.text = "/test"
    update.effective_message.reply_text = AsyncMock()
    update.effective_message.reply_html = AsyncMock()
    update.effective_message.answer = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 123456
    update.effective_chat.type = "private"
    update.effective_user = MagicMock()
    update.effective_user.id = 789012
    update.effective_user.first_name = "Test"
    update.effective_user.username = "testuser"
    return update
