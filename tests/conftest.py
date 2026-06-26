from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_PRE_IMPORT_DB = os.path.join(
    tempfile.gettempdir(), "newsdrop_conftest_init.db"
)
os.environ.setdefault("DATABASE_PATH", _PRE_IMPORT_DB)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SRC_PATH = os.path.join(_PROJECT_ROOT, "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

import pytest

from newsdrop import database as _database_mod
from newsdrop import news_fetcher as _news_fetcher_mod


def _cleanup_pre_import_db() -> None:
    if os.path.exists(_PRE_IMPORT_DB):
        try:
            os.unlink(_PRE_IMPORT_DB)
        except OSError:
            pass
    for ext in ("-wal", "-shm"):
        sidecar = _PRE_IMPORT_DB + ext
        if os.path.exists(sidecar):
            try:
                os.unlink(sidecar)
            except OSError:
                pass


_cleanup_pre_import_db()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    db_file_str = str(db_file)

    monkeypatch.setattr(_database_mod, "DB_PATH", Path(db_file_str))

    _database_mod._init_db()

    yield db_file_str


@pytest.fixture(autouse=True)
def _isolate_rate_limit_state():
    _news_fetcher_mod._daily_request_count = 0
    _news_fetcher_mod._cache.clear()
    yield
    _news_fetcher_mod._daily_request_count = 0
    _news_fetcher_mod._cache.clear()
