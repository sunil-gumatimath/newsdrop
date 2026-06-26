"""Shared pytest fixtures for the newsdrop test suite.

Notes on the DB setup dance
---------------------------
``database.py`` runs ``_init_db()`` at import time against the module global
``DB_PATH``. That means by the time any test runs, a database file has already
been created from whatever ``DATABASE_PATH`` was set to at process start.

To keep that first import from polluting the project directory, we set
``DATABASE_PATH`` to a temp file *before* importing the project modules. The
per-test ``tmp_db`` fixture then monkeypatches ``database.DB_PATH`` to a
unique per-test path and re-runs ``_init_db()`` to build a fresh schema.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 1. Pre-import: route the project's first DB init to a throwaway temp file.
_PRE_IMPORT_DB = os.path.join(
    tempfile.gettempdir(), "newsdrop_conftest_init.db"
)
os.environ.setdefault("DATABASE_PATH", _PRE_IMPORT_DB)

# 2. Make the project root importable so ``import database`` etc. work in tests.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 3. Now safe to import pytest and the project modules.
import pytest  # noqa: E402

import database as _database_mod  # noqa: E402
import news_fetcher as _news_fetcher_mod  # noqa: E402


def _cleanup_pre_import_db() -> None:
    """Remove the temp file the conftest's first DB init created."""
    if os.path.exists(_PRE_IMPORT_DB):
        try:
            os.unlink(_PRE_IMPORT_DB)
        except OSError:
            pass
    # WAL/SHM sidecar files (best-effort, ignore failures)
    for ext in ("-wal", "-shm"):
        sidecar = _PRE_IMPORT_DB + ext
        if os.path.exists(sidecar):
            try:
                os.unlink(sidecar)
            except OSError:
                pass


# Clean up the pre-import DB as soon as the test session starts.
_cleanup_pre_import_db()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Yield a path to a fresh SQLite file with the project schema applied.

    The fixture points ``database.DB_PATH`` at a per-test file and re-runs
    ``_init_db()`` so every test gets a clean schema. ``tmp_path`` is cleaned
    up automatically by pytest.
    """
    db_file = tmp_path / "test.db"
    db_file_str = str(db_file)

    # Re-point the module global that _get_connection() reads at call time.
    monkeypatch.setattr(_database_mod, "DB_PATH", Path(db_file_str))

    # Re-initialize the schema in the new file. This calls _create_schema and
    # _migrate_schema, which is exactly what a fresh install needs.
    _database_mod._init_db()

    yield db_file_str

    # tmp_path cleanup is automatic; just close any stragglers (no-op, defensive).
    # Connection lifecycle is managed inside database.py via try/finally.


@pytest.fixture(autouse=True)
def _isolate_rate_limit_state():
    """Reset news_fetcher's in-memory rate-limit + cache state between tests.

    Both are module-level globals and persist across tests otherwise, which
    would make order-dependent tests silently pass or fail.
    """
    _news_fetcher_mod._daily_request_count = 0
    _news_fetcher_mod._cache.clear()
    yield
    _news_fetcher_mod._daily_request_count = 0
    _news_fetcher_mod._cache.clear()
