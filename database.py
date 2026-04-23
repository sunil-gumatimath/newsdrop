"""SQLite-based storage for subscribers and user preferences.

This module provides simple, thread-safe SQLite access for:
- subscriber management
- user preference storage

It also performs lightweight schema migrations on startup so existing
databases can be brought in line with the current application schema.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot_data.db"
_lock = threading.Lock()


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
