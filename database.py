"""SQLite-based storage for subscribers, preferences, and followed topics.

This module provides simple, thread-safe SQLite access for:
- subscriber management
- user preference storage
- per-user followed topic storage

It also performs lightweight schema migrations on startup so existing
databases can be brought in line with the current application schema.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot_data.db"
_lock = threading.Lock()

MAX_FOLLOWED_TOPICS_PER_USER = 10


def _get_connection() -> sqlite3.Connection:
    """Return a SQLite connection configured for concurrent access."""
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
        conn.execute(
            "ALTER TABLE user_preferences ADD COLUMN country TEXT NOT NULL DEFAULT 'us'"
        )

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
    """Apply lightweight schema migrations."""
    if not _table_exists(conn, "subscribers"):
        conn.execute(
            """
            CREATE TABLE subscribers (
                chat_id INTEGER PRIMARY KEY
            )
            """
        )

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

            # Legacy safety migration for older DBs missing this column
            cursor = conn.execute("PRAGMA table_info(user_preferences)")
            columns = [row["name"] for row in cursor.fetchall()]
            if "breaking_news_enabled" not in columns:
                conn.execute(
                    """
                    ALTER TABLE user_preferences
                    ADD COLUMN breaking_news_enabled INTEGER NOT NULL DEFAULT 0
                    """
                )
                conn.commit()
        finally:
            conn.close()


def _normalize_topic(topic: str) -> str:
    """Normalize a followed topic for dedupe and comparison."""
    return " ".join(topic.strip().lower().split())


# Initialize on import
_init_db()


# ── Subscribers ──────────────────────────────────────────────────────


def load_subscribers() -> set[int]:
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute("SELECT chat_id FROM subscribers")
            return {row["chat_id"] for row in cursor.fetchall()}
        finally:
            conn.close()


def add_subscriber(chat_id: int) -> bool:
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


def remove_subscriber(chat_id: int) -> bool:
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


def is_subscriber(chat_id: int) -> bool:
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


# ── User Preferences ─────────────────────────────────────────────────


def get_user_prefs(chat_id: int, default_country: str = "us") -> dict[str, str]:
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


def set_user_prefs(
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


def get_breaking_news_preference(chat_id: int) -> bool:
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


def set_breaking_news_preference(chat_id: int, enabled: bool) -> None:
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


# ── Topic Follows ────────────────────────────────────────────────────


def get_followed_topics(chat_id: int) -> list[str]:
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


def is_following_topic(chat_id: int, topic: str) -> bool:
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


def add_followed_topic(chat_id: int, topic: str) -> tuple[bool, str]:
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


def remove_followed_topic(chat_id: int, topic: str) -> bool:
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


def clear_followed_topics(chat_id: int) -> int:
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


# ── Health ───────────────────────────────────────────────────────────


def check_db_health() -> dict[str, str]:
    """Check database health and return status information."""
    with _lock:
        conn = _get_connection()
        try:
            conn.execute("SELECT 1").fetchone()

            subscriber_row = conn.execute(
                "SELECT COUNT(*) AS count FROM subscribers"
            ).fetchone()
            topic_row = conn.execute(
                "SELECT COUNT(*) AS count FROM topic_follows"
            ).fetchone()

            subscriber_count = int(subscriber_row["count"]) if subscriber_row else 0
            topic_count = int(topic_row["count"]) if topic_row else 0

            return {
                "status": "healthy",
                "subscriber_count": str(subscriber_count),
                "followed_topic_count": str(topic_count),
            }
        except Exception as exc:
            return {
                "status": "unhealthy",
                "error": str(exc),
            }
        finally:
            conn.close()
