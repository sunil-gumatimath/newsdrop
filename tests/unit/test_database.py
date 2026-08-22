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
    assert expected.issubset(table_names), f"missing tables. expected {expected}, got {table_names}"


async def test_followed_topics_unique_per_user(tmp_db):
    chat_id = 12345

    created1, msg1 = await database.add_followed_topic(chat_id, "Bitcoin")
    created2, msg2 = await database.add_followed_topic(chat_id, "bitcoin")

    assert created1 is True, f"first add should succeed, got: {msg1!r}"
    assert created2 is False, f"second add should be deduped, got: {msg2!r}"
    assert "already" in msg2.lower(), f"expected 'already' in dedupe message, got: {msg2!r}"

    topics = await database.get_followed_topics(chat_id)
    assert len(topics) == 1, f"expected exactly 1 stored topic, got {topics!r}"


async def test_max_followed_topics_enforced(tmp_db):
    chat_id = 99999

    for i in range(10):
        ok, msg = await database.add_followed_topic(chat_id, f"topic-{i}")
        assert ok, f"topic-{i} should have been added, got: {msg!r}"

    ok, msg = await database.add_followed_topic(chat_id, "topic-10")
    assert ok is False, "11th topic should be rejected by MAX_FOLLOWED_TOPICS_PER_USER"
    assert "10" in msg, f"expected the limit (10) in the rejection message, got: {msg!r}"

    topics = await database.get_followed_topics(chat_id)
    assert len(topics) == 10, f"expected 10 stored topics, got {len(topics)}: {topics!r}"


async def test_schedule_and_breaking_keyword_prefs(tmp_db):
    chat_id = 42
    await database.set_user_prefs(
        chat_id,
        timezone="America/New_York",
        daily_hour=18,
        quiet_start_hour=22,
        quiet_end_hour=7,
        breaking_keywords="AI, climate",
        breaking_use_follows=False,
    )
    prefs = await database.get_user_prefs(chat_id)
    assert prefs["timezone"] == "America/New_York"
    assert prefs["daily_hour"] == "18"
    assert prefs["quiet_start_hour"] == "22"
    assert prefs["quiet_end_hour"] == "7"
    assert prefs["breaking_use_follows"] == "0"
    assert database.parse_breaking_keywords(prefs["breaking_keywords"]) == ["AI", "climate"]

    await database.set_user_prefs(chat_id, clear_quiet_hours=True)
    prefs = await database.get_user_prefs(chat_id)
    assert prefs["quiet_start_hour"] == ""
    assert prefs["quiet_end_hour"] == ""


async def test_default_country_honored_for_new_user(tmp_db):
    """Regression: a new user's stored country must honor DEFAULT_COUNTRY.

    _set_user_prefs_sync previously fell back to the hardcoded "us" default
    instead of the configured default_country, so e.g. DEFAULT_COUNTRY=in
    would silently store "us".
    """
    chat_id = 4242
    prefs = await database.set_user_prefs(chat_id, timezone="Asia/Kolkata", default_country="in")
    assert prefs["country"] == "in"

    reread = await database.get_user_prefs(chat_id, default_country="in")
    assert reread["country"] == "in"
    await database.remove_subscriber(chat_id)
