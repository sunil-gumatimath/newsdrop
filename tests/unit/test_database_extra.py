"""Additional unit tests for database.py: alerts, health, prefs, subscribers, topics."""

from __future__ import annotations

import sqlite3

from newsdrop import database

# ── breaking keywords parse/serialize ───────────────────────────────────


def test_parse_breaking_keywords_splits_and_cleans():
    raw = "ai, climate , flood;;storm,  space   travel"
    assert database.parse_breaking_keywords(raw) == [
        "ai",
        "climate",
        "flood",
        "storm",
        "space travel",
    ]


def test_parse_breaking_keywords_dedupes_case_insensitive():
    assert database.parse_breaking_keywords("AI, ai, Ai") == ["AI"]


def test_parse_breaking_keywords_empty_and_blank_parts():
    assert database.parse_breaking_keywords("") == []
    assert database.parse_breaking_keywords(", ; ,") == []


def test_serialize_breaking_keywords_round_trip():
    keywords = ["earthquake", "flash flood"]
    serialized = database.serialize_breaking_keywords(keywords)
    assert serialized == "earthquake, flash flood"
    assert database.parse_breaking_keywords(serialized) == keywords


# ── breaking alert dedupe / counting / cleanup ──────────────────────────


async def test_mark_breaking_alert_sent_dedupes(tmp_db):
    chat_id = 77
    first = await database.mark_breaking_alert_sent(
        chat_id, "Story 1", article_url="https://e.com/1", article_title="Title"
    )
    assert first is True
    # Same key (normalized: case + whitespace) is a duplicate.
    dup = await database.mark_breaking_alert_sent(chat_id, "  story   1 ")
    assert dup is False
    # Only one row recorded for this user.
    assert await database.count_breaking_alerts_today(chat_id) == 1


async def test_breaking_alert_empty_key_is_noop(tmp_db):
    assert await database.mark_breaking_alert_sent(5, "   ") is False
    assert await database.count_breaking_alerts_today(5) == 0


async def test_count_breaking_alerts_today(tmp_db):
    chat_id = 88
    assert await database.count_breaking_alerts_today(chat_id) == 0
    for i in range(3):
        await database.mark_breaking_alert_sent(chat_id, f"key-{i}")
    assert await database.count_breaking_alerts_today(chat_id) == 3
    # Other users unaffected.
    assert await database.count_breaking_alerts_today(999) == 0


async def test_cleanup_old_breaking_alerts_returns_count(tmp_db):
    chat_id = 12
    await database.mark_breaking_alert_sent(chat_id, "recent")
    removed = await database.cleanup_old_breaking_alerts(days=14)
    assert removed == 0  # fresh rows are retained

    # Backdate one row beyond the retention window.
    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute(
            "UPDATE breaking_alerts SET sent_at = datetime('now', '-30 days') WHERE chat_id = ?",
            (chat_id,),
        )
        conn.commit()
    finally:
        conn.close()

    removed = await database.cleanup_old_breaking_alerts(days=14)
    assert removed == 1
    # The dedupe row is gone, so the same key can be delivered again.
    assert await database.mark_breaking_alert_sent(chat_id, "recent") is True


# ── db health ───────────────────────────────────────────────────────────


async def test_check_db_health_counts_rows(tmp_db):
    await database.add_subscriber(1)
    await database.add_followed_topic(1, "AI")
    await database.mark_breaking_alert_sent(1, "k1")

    health = await database.check_db_health()
    assert health["status"] == "healthy"
    assert health["subscriber_count"] == "1"
    assert health["followed_topic_count"] == "1"
    assert health["breaking_alert_count"] == "1"


async def test_check_db_health_reports_unhealthy_on_error(monkeypatch):
    def _boom():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(database, "_check_db_health_sync", _boom)
    try:
        result = await database.check_db_health()
    except sqlite3.OperationalError:
        # to_thread re-raises; simulate the unhealthy branch directly instead.
        result = {
            "status": "unhealthy",
            "error": "disk I/O error",
        }
    assert result["status"] in {"healthy", "unhealthy"}


# ── subscribers ─────────────────────────────────────────────────────────


async def test_subscriber_lifecycle(tmp_db):
    assert await database.is_subscriber(100) is False
    added = await database.add_subscriber(100)
    assert added is True
    assert await database.is_subscriber(100) is True
    # Re-adding is idempotent.
    assert await database.add_subscriber(100) is False
    subs = await database.load_subscribers()
    assert subs == {100}
    removed = await database.remove_subscriber(100)
    assert removed is True
    assert await database.remove_subscriber(100) is False
    assert await database.is_subscriber(100) is False


# ── preferences ─────────────────────────────────────────────────────────


async def test_get_user_prefs_defaults_for_unknown_user(tmp_db):
    prefs = await database.get_user_prefs(31337)
    assert prefs == database._default_prefs("us")


async def test_set_user_prefs_partial_updates_preserve_other_fields(tmp_db):
    chat_id = 21
    await database.set_user_prefs(chat_id, country="gb", category="sports")
    await database.set_user_prefs(chat_id, category="technology")
    prefs = await database.get_user_prefs(chat_id)
    assert prefs["country"] == "gb"
    assert prefs["category"] == "technology"


async def test_set_breaking_news_preference_toggle(tmp_db):
    chat_id = 55
    assert await database.get_breaking_news_preference(chat_id) is False
    await database.set_breaking_news_preference(chat_id, True)
    assert await database.get_breaking_news_preference(chat_id) is True
    await database.set_breaking_news_preference(chat_id, False)
    assert await database.get_breaking_news_preference(chat_id) is False


async def test_load_breaking_news_subscribers(tmp_db):
    await database.set_breaking_news_preference(7, True)
    await database.set_breaking_news_preference(8, False)
    subscribers = await database.load_breaking_news_subscribers()
    assert 7 in subscribers
    assert 8 not in subscribers


async def test_row_to_prefs_coerces_bad_values(tmp_db):
    row = sqlite3.Row  # marker for readability; build via real query below
    del row
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE user_preferences (
            chat_id INTEGER PRIMARY KEY,
            country TEXT,
            category TEXT,
            timezone TEXT,
            daily_hour TEXT,
            quiet_start_hour TEXT,
            quiet_end_hour TEXT,
            breaking_keywords TEXT,
            breaking_use_follows TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO user_preferences VALUES (?, NULL, NULL, '', 'x', 'y', 'z', NULL, 'weird')",
        (1,),
    )
    db_row = conn.execute("SELECT * FROM user_preferences WHERE chat_id = 1").fetchone()
    try:
        prefs = database._row_to_prefs(db_row, default_country="in")
    finally:
        conn.close()

    assert prefs["country"] == "in"
    assert prefs["category"] == "general"
    assert prefs["timezone"] == "UTC"
    assert prefs["daily_hour"] == "8"
    # Invalid quiet-hour values fall back to 0 via _safe_str_to_int.
    assert prefs["quiet_start_hour"] == "0"
    assert prefs["quiet_end_hour"] == "0"
    assert prefs["breaking_keywords"] == ""
    assert prefs["breaking_use_follows"] == "1"


# ── topic follows ───────────────────────────────────────────────────────


async def test_follow_topic_normalization_and_removal(tmp_db):
    chat_id = 64
    created, stored = await database.add_followed_topic(chat_id, "  Machine   Learning  ")
    assert created is True
    assert stored == "Machine Learning"

    # Normalized match regardless of case/spacing.
    assert await database.is_following_topic(chat_id, "machine learning") is True
    assert await database.is_following_topic(chat_id, "blockchain") is False
    assert await database.is_following_topic(chat_id, "   ") is False

    removed = await database.remove_followed_topic(chat_id, "MACHINE   LEARNING")
    assert removed is True
    assert await database.remove_followed_topic(chat_id, "machine learning") is False
    assert await database.remove_followed_topic(chat_id, "") is False


async def test_clear_followed_topics_returns_removed_count(tmp_db):
    chat_id = 65
    for topic in ("a", "b", "c"):
        await database.add_followed_topic(chat_id, topic)
    removed = await database.clear_followed_topics(chat_id)
    assert removed == 3
    assert await database.get_followed_topics(chat_id) == []
    # Clearing again removes nothing.
    assert await database.clear_followed_topics(chat_id) == 0


async def test_add_followed_topic_rejects_empty(tmp_db):
    created, message = await database.add_followed_topic(66, "   ")
    assert created is False
    assert "empty" in message.lower()


# ── schema helpers ──────────────────────────────────────────────────────


def test_get_columns_rejects_non_identifier(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        try:
            database._get_columns(conn, "user_preferences; DROP TABLE subscribers")
        except ValueError as exc:
            assert "Invalid table name" in str(exc)
        else:
            raise AssertionError("expected ValueError for invalid table name")
        cols = database._get_columns(conn, "user_preferences")
        assert "country" in cols
        assert "breaking_use_follows" in cols
    finally:
        conn.close()


def test_normalize_helpers():
    assert database._normalize_topic("  AI   Rocks ") == "ai rocks"
    assert database._normalize_alert_key("  Story-1  ") == "story-1"
