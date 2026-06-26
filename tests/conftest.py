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

import pytest

from newsdrop import database as _database_mod
from newsdrop import news_fetcher as _news_fetcher_mod


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

    _database_mod._init_db()

    yield db_file_str


@pytest.fixture(autouse=True)
def _isolate_rate_limit_state():
    _news_fetcher_mod._daily_request_count = 0
    _news_fetcher_mod._cache.clear()
    yield
    _news_fetcher_mod._daily_request_count = 0
    _news_fetcher_mod._cache.clear()
