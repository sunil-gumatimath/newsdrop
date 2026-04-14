"""SQLite-based storage for subscribers and user preferences.

Replaces the previous JSON file approach with atomic, concurrent-safe operations.
"""

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot_data.db"
_lock = threading.Lock()


def _get_connection() -> sqlite3.Connection:
    """Return a thread-safe connection with WAL mode for concurrency."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db() -> None:
    """Create tables if they don't exist. Idempotent."""
    with _lock:
        conn = _get_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS user_preferences (
                    chat_id INTEGER PRIMARY KEY,
                    country TEXT NOT NULL DEFAULT 'us',
                    category TEXT NOT NULL DEFAULT 'general',
                    FOREIGN KEY (chat_id) REFERENCES subscribers(chat_id)
                        ON DELETE CASCADE
                );
                """
            )
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
            return {row[0] for row in cursor.fetchall()}
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
                "DELETE FROM subscribers WHERE chat_id = ?", (chat_id,)
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
                "SELECT 1 FROM subscribers WHERE chat_id = ?", (chat_id,)
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()


# ── User Preferences ─────────────────────────────────────────────────

def get_user_prefs(
    chat_id: int, default_country: str = "us"
) -> dict[str, str]:
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                "SELECT country, category FROM user_preferences WHERE chat_id = ?",
                (chat_id,),
            )
            row = cursor.fetchone()
            if row:
                return {"country": row[0], "category": row[1]}
            return {"country": default_country, "category": "general"}
        finally:
            conn.close()


def set_user_prefs(
    chat_id: int, country: str = None, category: str = None
) -> dict[str, str]:
    """Upsert user preferences. Only provided fields are updated."""
    with _lock:
        conn = _get_connection()
        try:
            # Check if row exists
            cursor = conn.execute(
                "SELECT country, category FROM user_preferences WHERE chat_id = ?",
                (chat_id,),
            )
            row = cursor.fetchone()

            if row:
                current = {"country": row[0], "category": row[1]}
            else:
                current = {"country": "us", "category": "general"}

            if country is not None:
                current["country"] = country
            if category is not None:
                current["category"] = category

            conn.execute(
                """
                INSERT INTO user_preferences (chat_id, country, category)
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
