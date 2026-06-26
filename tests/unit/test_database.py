"""Unit tests for the SQLite storage layer in ``database.py``.

These tests use the ``tmp_db`` fixture to get a clean, per-test schema. The
production ``database.py`` keeps ``DB_PATH`` as a module global, so the
fixture monkeypatches it before re-running ``_init_db()`` to build a fresh
file.
"""

from __future__ import annotations

import sqlite3

import pytest

import database


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_init_db_creates_schema(tmp_db):
    """``_init_db`` on an empty file should create all 4 expected tables."""
    conn = sqlite3.connect(tmp_db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        table_names = {row[0] for row in rows}
    finally:
        conn.close()

    # These are the tables _create_schema() / _migrate_schema() promise.
    expected = {
        "subscribers",
        "user_preferences",
        "topic_follows",
        "breaking_alerts",
    }
    assert expected.issubset(table_names), (
        f"missing tables. expected {expected}, got {table_names}"
    )


def test_followed_topics_unique_per_user(tmp_db):
    """Adding the same topic twice should dedupe (single row, single message)."""
    chat_id = 12345

    created1, msg1 = database.add_followed_topic(chat_id, "Bitcoin")
    created2, msg2 = database.add_followed_topic(chat_id, "bitcoin")  # case-only diff

    # The first insert should succeed; the second must report "already following"
    # (dedupe is enforced at the application layer *and* the PK on
    # (chat_id, topic_normalized)).
    assert created1 is True, f"first add should succeed, got: {msg1!r}"
    assert created2 is False, f"second add should be deduped, got: {msg2!r}"
    assert "already" in msg2.lower(), f"expected 'already' in dedupe message, got: {msg2!r}"

    topics = database.get_followed_topics(chat_id)
    assert len(topics) == 1, f"expected exactly 1 stored topic, got {topics!r}"


def test_max_followed_topics_enforced(tmp_db):
    """The 11th distinct topic must be rejected (limit is 10)."""
    chat_id = 99999

    # First 10 should all succeed.
    for i in range(10):
        ok, msg = database.add_followed_topic(chat_id, f"topic-{i}")
        assert ok, f"topic-{i} should have been added, got: {msg!r}"

    # 11th must be rejected.
    ok, msg = database.add_followed_topic(chat_id, "topic-10")
    assert ok is False, "11th topic should be rejected by MAX_FOLLOWED_TOPICS_PER_USER"
    assert "10" in msg, f"expected the limit (10) in the rejection message, got: {msg!r}"

    # Confirm we have exactly 10 stored.
    topics = database.get_followed_topics(chat_id)
    assert len(topics) == 10, f"expected 10 stored topics, got {len(topics)}: {topics!r}"
