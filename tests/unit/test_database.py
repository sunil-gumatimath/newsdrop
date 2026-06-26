from __future__ import annotations

import sqlite3

from newsdrop import database


def test_init_db_creates_schema(tmp_db):
    conn = sqlite3.connect(tmp_db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        table_names = {row[0] for row in rows}
    finally:
        conn.close()

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
    chat_id = 12345

    created1, msg1 = database.add_followed_topic(chat_id, "Bitcoin")
    created2, msg2 = database.add_followed_topic(chat_id, "bitcoin")

    assert created1 is True, f"first add should succeed, got: {msg1!r}"
    assert created2 is False, f"second add should be deduped, got: {msg2!r}"
    assert "already" in msg2.lower(), f"expected 'already' in dedupe message, got: {msg2!r}"

    topics = database.get_followed_topics(chat_id)
    assert len(topics) == 1, f"expected exactly 1 stored topic, got {topics!r}"


def test_max_followed_topics_enforced(tmp_db):
    chat_id = 99999

    for i in range(10):
        ok, msg = database.add_followed_topic(chat_id, f"topic-{i}")
        assert ok, f"topic-{i} should have been added, got: {msg!r}"

    ok, msg = database.add_followed_topic(chat_id, "topic-10")
    assert ok is False, "11th topic should be rejected by MAX_FOLLOWED_TOPICS_PER_USER"
    assert "10" in msg, f"expected the limit (10) in the rejection message, got: {msg!r}"

    topics = database.get_followed_topics(chat_id)
    assert len(topics) == 10, f"expected 10 stored topics, got {len(topics)}: {topics!r}"
