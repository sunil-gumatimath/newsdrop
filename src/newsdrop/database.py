"""SQLite-based storage for subscribers, preferences, followed topics, and alerts.

This module provides simple, thread-safe SQLite access for:
- subscriber management
- user preference storage
- per-user followed topic storage
- persistent breaking-alert delivery tracking

It also performs lightweight schema migrations on startup so existing
databases can be brought in line with the current application schema.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from pathlib import Path


def _resolve_db_path() -> Path:
    """Return the configured SQLite database path.

    Set DATABASE_PATH to store the database somewhere outside the app directory,
    for example /app/data/bot_data.db when running in Docker.
    """
    configured_path = os.getenv("DATABASE_PATH")
    if configured_path:
        return Path(configured_path).expanduser()
    # Default to <project-root>/data/bot_data.db
    project_root = Path(__file__).resolve().parents[2]
    return project_root / "data" / "bot_data.db"


DB_PATH = _resolve_db_path()
_lock = threading.Lock()

MAX_FOLLOWED_TOPICS_PER_USER = 10


def _get_connection() -> sqlite3.Connection:
    """Return a SQLite connection configured for concurrent access."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _get_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the latest schema for fresh installs."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS user_preferences (
            chat_id INTEGER PRIMARY KEY,
            country TEXT NOT NULL DEFAULT 'us',
            category TEXT NOT NULL DEFAULT 'general',
            breaking_news_enabled INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS topic_follows (
            chat_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            topic_normalized TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, topic_normalized)
        );

        CREATE TABLE IF NOT EXISTS breaking_alerts (
            chat_id INTEGER NOT NULL,
            article_key TEXT NOT NULL,
            article_url TEXT NOT NULL DEFAULT '',
            article_title TEXT NOT NULL DEFAULT '',
            sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, article_key)
        );

        CREATE INDEX IF NOT EXISTS idx_breaking_alerts_sent_at
        ON breaking_alerts (sent_at);
        """
    )


def _migrate_user_preferences(conn: sqlite3.Connection) -> None:
    """Bring the user_preferences table up to the current schema."""
    if not _table_exists(conn, "user_preferences"):
        conn.execute(
            """
            CREATE TABLE user_preferences (
                chat_id INTEGER PRIMARY KEY,
                country TEXT NOT NULL DEFAULT 'us',
                category TEXT NOT NULL DEFAULT 'general',
                breaking_news_enabled INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        return

    columns = _get_columns(conn, "user_preferences")

    if "country" not in columns:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN country TEXT NOT NULL DEFAULT 'us'")

    if "category" not in columns:
        conn.execute(
            """
            ALTER TABLE user_preferences
            ADD COLUMN category TEXT NOT NULL DEFAULT 'general'
            """
        )

    if "breaking_news_enabled" not in columns:
        conn.execute(
            """
            ALTER TABLE user_preferences
            ADD COLUMN breaking_news_enabled INTEGER NOT NULL DEFAULT 0
            """
        )


def _migrate_topic_follows(conn: sqlite3.Connection) -> None:
    """Bring the topic_follows table up to the current schema."""
    if not _table_exists(conn, "topic_follows"):
        conn.execute(
            """
            CREATE TABLE topic_follows (
                chat_id INTEGER NOT NULL,
                topic TEXT NOT NULL,
                topic_normalized TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, topic_normalized)
            )
            """
        )
        return

    columns = _get_columns(conn, "topic_follows")

    if "topic_normalized" not in columns:
        conn.execute(
            """
            ALTER TABLE topic_follows
            ADD COLUMN topic_normalized TEXT NOT NULL DEFAULT ''
            """
        )
        conn.execute(
            """
            UPDATE topic_follows
            SET topic_normalized = LOWER(TRIM(topic))
            WHERE topic_normalized = ''
            """
        )

    if "created_at" not in columns:
        conn.execute(
            """
            ALTER TABLE topic_follows
            ADD COLUMN created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            """
        )


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply lightweight schema migrations.

    Note: ``_create_schema()`` is always called first and handles all
    ``CREATE TABLE IF NOT EXISTS`` statements. This function only adds
    columns that may be missing from older databases and does not repeat
    table creation or index creation already done by ``_create_schema``.
    """
    _migrate_user_preferences(conn)
    _migrate_topic_follows(conn)


def _init_db() -> None:
    """Initialize database and apply schema migrations."""
    with _lock:
        conn = _get_connection()
        try:
            _create_schema(conn)
            _migrate_schema(conn)
            conn.commit()
        finally:
            conn.close()


def _normalize_topic(topic: str) -> str:
    """Normalize a followed topic for dedupe and comparison."""
    return " ".join(topic.strip().lower().split())


def _normalize_alert_key(article_key: str) -> str:
    """Normalize an article identifier for breaking-alert dedupe."""
    return " ".join(article_key.strip().lower().split())


# Initialize on import
_init_db()


# ── Subscribers ──────────────────────────────────────────────────────


def _load_subscribers_sync() -> set[int]:
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute("SELECT chat_id FROM subscribers")
            return {row["chat_id"] for row in cursor.fetchall()}
        finally:
            conn.close()


async def load_subscribers() -> set[int]:
    return await asyncio.to_thread(_load_subscribers_sync)


def _add_subscriber_sync(chat_id: int) -> bool:
    """Add a subscriber. Returns True if newly added, False if already exists."""
    with _lock:
        conn = _get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)",
                (chat_id,),
            )
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()


async def add_subscriber(chat_id: int) -> bool:
    return await asyncio.to_thread(_add_subscriber_sync, chat_id)


def _remove_subscriber_sync(chat_id: int) -> bool:
    """Remove a subscriber. Returns True if removed, False if not found."""
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


async def remove_subscriber(chat_id: int) -> bool:
    return await asyncio.to_thread(_remove_subscriber_sync, chat_id)


def _is_subscriber_sync(chat_id: int) -> bool:
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                "SELECT 1 FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()


async def is_subscriber(chat_id: int) -> bool:
    return await asyncio.to_thread(_is_subscriber_sync, chat_id)


# ── User Preferences ─────────────────────────────────────────────────


def _get_user_prefs_sync(chat_id: int, default_country: str = "us") -> dict[str, str]:
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT country, category
                FROM user_preferences
                WHERE chat_id = ?
                """,
                (chat_id,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "country": row["country"],
                    "category": row["category"],
                }
            return {"country": default_country, "category": "general"}
        finally:
            conn.close()


async def get_user_prefs(chat_id: int, default_country: str = "us") -> dict[str, str]:
    return await asyncio.to_thread(_get_user_prefs_sync, chat_id, default_country)


def _set_user_prefs_sync(
    chat_id: int,
    country: str | None = None,
    category: str | None = None,
) -> dict[str, str]:
    """Upsert user preferences. Only provided fields are updated."""
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT country, category
                FROM user_preferences
                WHERE chat_id = ?
                """,
                (chat_id,),
            )
            row = cursor.fetchone()

            if row:
                current = {
                    "country": row["country"],
                    "category": row["category"],
                }
            else:
                current = {
                    "country": "us",
                    "category": "general",
                }

            if country is not None:
                current["country"] = country
            if category is not None:
                current["category"] = category

            conn.execute(
                """
                INSERT INTO user_preferences (
                    chat_id,
                    country,
                    category
                )
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    country = excluded.country,
                    category = excluded.category
                """,
                (chat_id, current["country"], current["category"]),
            )
            conn.commit()
            return current
        finally:
            conn.close()


async def set_user_prefs(
    chat_id: int,
    country: str | None = None,
    category: str | None = None,
) -> dict[str, str]:
    return await asyncio.to_thread(_set_user_prefs_sync, chat_id, country, category)


def _get_breaking_news_preference_sync(chat_id: int) -> bool:
    """Get breaking news preference for a user."""
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT breaking_news_enabled
                FROM user_preferences
                WHERE chat_id = ?
                """,
                (chat_id,),
            )
            row = cursor.fetchone()
            if row:
                return bool(row["breaking_news_enabled"])
            return False
        finally:
            conn.close()


async def get_breaking_news_preference(chat_id: int) -> bool:
    return await asyncio.to_thread(_get_breaking_news_preference_sync, chat_id)


def _set_breaking_news_preference_sync(chat_id: int, enabled: bool) -> None:
    """Set breaking news preference for a user."""
    with _lock:
        conn = _get_connection()
        try:
            conn.execute(
                """
                INSERT INTO user_preferences (chat_id, breaking_news_enabled)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    breaking_news_enabled = excluded.breaking_news_enabled
                """,
                (chat_id, 1 if enabled else 0),
            )
            conn.commit()
        finally:
            conn.close()


async def set_breaking_news_preference(chat_id: int, enabled: bool) -> None:
    return await asyncio.to_thread(_set_breaking_news_preference_sync, chat_id, enabled)


def _load_breaking_news_subscribers_sync() -> set[int]:
    """Return chat IDs for users who opted into breaking-news alerts."""
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT chat_id
                FROM user_preferences
                WHERE breaking_news_enabled = 1
                """
            )
            return {row["chat_id"] for row in cursor.fetchall()}
        finally:
            conn.close()


async def load_breaking_news_subscribers() -> set[int]:
    return await asyncio.to_thread(_load_breaking_news_subscribers_sync)


def _was_breaking_alert_sent_sync(chat_id: int, article_key: str) -> bool:
    """Return True if a breaking alert was already sent to this user."""
    normalized_key = _normalize_alert_key(article_key)
    if not normalized_key:
        return False

    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT 1
                FROM breaking_alerts
                WHERE chat_id = ? AND article_key = ?
                """,
                (chat_id, normalized_key),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()


async def was_breaking_alert_sent(chat_id: int, article_key: str) -> bool:
    return await asyncio.to_thread(_was_breaking_alert_sent_sync, chat_id, article_key)


def _mark_breaking_alert_sent_sync(
    chat_id: int,
    article_key: str,
    article_url: str = "",
    article_title: str = "",
) -> bool:
    """Persist that a breaking alert was sent.

    Returns True when this is the first recorded delivery for the user/article.
    """
    normalized_key = _normalize_alert_key(article_key)
    if not normalized_key:
        return False

    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO breaking_alerts (
                    chat_id,
                    article_key,
                    article_url,
                    article_title
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    chat_id,
                    normalized_key,
                    article_url.strip(),
                    article_title.strip(),
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


async def mark_breaking_alert_sent(
    chat_id: int,
    article_key: str,
    article_url: str = "",
    article_title: str = "",
) -> bool:
    return await asyncio.to_thread(
        _mark_breaking_alert_sent_sync,
        chat_id,
        article_key,
        article_url,
        article_title,
    )


def _cleanup_old_breaking_alerts_sync(days: int = 14) -> int:
    """Delete old breaking-alert tracking rows and return the number removed."""
    retention_days = max(days, 1)

    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                DELETE FROM breaking_alerts
                WHERE sent_at < datetime('now', ?)
                """,
                (f"-{retention_days} days",),
            )
            conn.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0
        finally:
            conn.close()


async def cleanup_old_breaking_alerts(days: int = 14) -> int:
    return await asyncio.to_thread(_cleanup_old_breaking_alerts_sync, days)


# ── Topic Follows ────────────────────────────────────────────────────


def _get_followed_topics_sync(chat_id: int) -> list[str]:
    """Return followed topics for a user in creation order."""
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT topic
                FROM topic_follows
                WHERE chat_id = ?
                ORDER BY created_at ASC, topic ASC
                """,
                (chat_id,),
            )
            return [str(row["topic"]) for row in cursor.fetchall()]
        finally:
            conn.close()


async def get_followed_topics(chat_id: int) -> list[str]:
    return await asyncio.to_thread(_get_followed_topics_sync, chat_id)


def _is_following_topic_sync(chat_id: int, topic: str) -> bool:
    """Return True if a user already follows the given topic."""
    normalized = _normalize_topic(topic)
    if not normalized:
        return False

    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT 1
                FROM topic_follows
                WHERE chat_id = ? AND topic_normalized = ?
                """,
                (chat_id, normalized),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()


async def is_following_topic(chat_id: int, topic: str) -> bool:
    return await asyncio.to_thread(_is_following_topic_sync, chat_id, topic)


def _add_followed_topic_sync(chat_id: int, topic: str) -> tuple[bool, str]:
    """Add a followed topic.

    Returns:
        (created, message)
        - created=True if the topic was newly added
        - created=False with a reason if it already exists or input is invalid
    """
    cleaned_topic = " ".join(topic.strip().split())
    normalized = _normalize_topic(cleaned_topic)

    if not normalized:
        return False, "Topic cannot be empty."

    with _lock:
        conn = _get_connection()
        try:
            existing_count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM topic_follows
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()

            topic_count = int(existing_count["count"]) if existing_count else 0

            existing = conn.execute(
                """
                SELECT 1
                FROM topic_follows
                WHERE chat_id = ? AND topic_normalized = ?
                """,
                (chat_id, normalized),
            ).fetchone()

            if existing is not None:
                return False, "You already follow that topic."

            if topic_count >= MAX_FOLLOWED_TOPICS_PER_USER:
                return (
                    False,
                    f"You can follow up to {MAX_FOLLOWED_TOPICS_PER_USER} topics.",
                )

            conn.execute(
                """
                INSERT INTO topic_follows (chat_id, topic, topic_normalized)
                VALUES (?, ?, ?)
                """,
                (chat_id, cleaned_topic, normalized),
            )
            conn.commit()
            return True, cleaned_topic
        finally:
            conn.close()


async def add_followed_topic(chat_id: int, topic: str) -> tuple[bool, str]:
    return await asyncio.to_thread(_add_followed_topic_sync, chat_id, topic)


def _remove_followed_topic_sync(chat_id: int, topic: str) -> bool:
    """Remove a followed topic. Returns True if removed, False if not found."""
    normalized = _normalize_topic(topic)
    if not normalized:
        return False

    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                DELETE FROM topic_follows
                WHERE chat_id = ? AND topic_normalized = ?
                """,
                (chat_id, normalized),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


async def remove_followed_topic(chat_id: int, topic: str) -> bool:
    return await asyncio.to_thread(_remove_followed_topic_sync, chat_id, topic)


def _clear_followed_topics_sync(chat_id: int) -> int:
    """Remove all followed topics for a user. Returns number removed."""
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM topic_follows WHERE chat_id = ?",
                (chat_id,),
            )
            conn.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0
        finally:
            conn.close()


async def clear_followed_topics(chat_id: int) -> int:
    return await asyncio.to_thread(_clear_followed_topics_sync, chat_id)


# ── Health ───────────────────────────────────────────────────────────


def _check_db_health_sync() -> dict[str, str]:
    """Check database health and return status information."""
    with _lock:
        conn = _get_connection()
        try:
            conn.execute("SELECT 1").fetchone()

            subscriber_row = conn.execute("SELECT COUNT(*) AS count FROM subscribers").fetchone()
            topic_row = conn.execute("SELECT COUNT(*) AS count FROM topic_follows").fetchone()
            alert_row = conn.execute("SELECT COUNT(*) AS count FROM breaking_alerts").fetchone()

            subscriber_count = int(subscriber_row["count"]) if subscriber_row else 0
            topic_count = int(topic_row["count"]) if topic_row else 0
            alert_count = int(alert_row["count"]) if alert_row else 0

            return {
                "status": "healthy",
                "subscriber_count": str(subscriber_count),
                "followed_topic_count": str(topic_count),
                "breaking_alert_count": str(alert_count),
            }
        except Exception as exc:
            return {
                "status": "unhealthy",
                "error": str(exc),
            }
        finally:
            conn.close()


async def check_db_health() -> dict[str, str]:
    return await asyncio.to_thread(_check_db_health_sync)
