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
import logging
import os
import sqlite3
import tempfile
import threading
from pathlib import Path

from .config import DEFAULT_COUNTRY

logger = logging.getLogger(__name__)


def _resolve_db_path() -> Path:
    """Return the configured SQLite database path.

    Set DATABASE_PATH to store the database somewhere outside the app directory,
    for example /app/data/bot_data.db when running in Docker.

    Security: the path is canonicalized via ``Path.resolve()`` and checked
    to lie under an allowed prefix (project ``data/``, ``/app/data``, or the
    system temp dir for tests). Paths outside these prefixes are rejected
    with a warning and the default is used instead — this prevents
    directory-traversal or arbitrary file writes via a crafted DATABASE_PATH.
    """
    configured_path = os.getenv("DATABASE_PATH")
    if configured_path and configured_path.strip():
        raw = Path(configured_path.strip()).expanduser()
        try:
            resolved = raw.resolve()
        except Exception:
            logger.warning("Failed to resolve DATABASE_PATH=%r, using as-is", configured_path)
            resolved = raw.absolute()

        project_root = Path(__file__).resolve().parents[2]
        # Allowed bases: project data dir, /app/data (Docker), project root itself,
        # and temp directory (used by tests via tmp_path / conftest).
        candidate_bases = [
            project_root / "data",
            Path("/app/data"),
            project_root,
            Path(tempfile.gettempdir()),
        ]
        allowed_bases: list[Path] = []
        for base in candidate_bases:
            try:
                allowed_bases.append(base.resolve())
            except Exception:
                allowed_bases.append(base)

        is_allowed = False
        for base in allowed_bases:
            try:
                # Python 3.9+: Path.is_relative_to
                if hasattr(resolved, "is_relative_to"):
                    if resolved.is_relative_to(base):
                        is_allowed = True
                        break
                else:
                    resolved.relative_to(base)
                    is_allowed = True
                    break
            except ValueError:
                continue

        if not is_allowed:
            logger.warning(
                "DATABASE_PATH %r resolves to %r outside allowed prefixes %r; "
                "falling back to default <project-root>/data/bot_data.db",
                configured_path,
                str(resolved),
                [str(b) for b in allowed_bases],
            )
            return (project_root / "data" / "bot_data.db").resolve()
        return resolved
    # Default to <project-root>/data/bot_data.db
    project_root = Path(__file__).resolve().parents[2]
    return (project_root / "data" / "bot_data.db").resolve()


DB_PATH = _resolve_db_path()
_lock = threading.Lock()

MAX_FOLLOWED_TOPICS_PER_USER = 10


def _get_connection() -> sqlite3.Connection:
    """Open a fresh SQLite connection for the current DB operation.

    A new connection is created per call. This is simple and correct: every
    ``_*_sync`` helper closes its connection in a ``finally`` block, so no
    connection is ever reused across operations (and thus ``conn.total_changes``
    in ``_add_subscriber_sync`` reflects only the current statement).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # busy_timeout avoids SQLITE_BUSY on concurrent writers (WAL + timeout).
    conn.execute("PRAGMA busy_timeout=5000")
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
    # Validate table_name is a bare SQL identifier (prevents injection even
    # though callers only pass internal constants).
    if not table_name or not table_name.replace("_", "").isalnum():
        raise ValueError(f"Invalid table name: {table_name!r}")
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
            breaking_news_enabled INTEGER NOT NULL DEFAULT 0,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            daily_hour INTEGER NOT NULL DEFAULT 8,
            quiet_start_hour INTEGER,
            quiet_end_hour INTEGER,
            breaking_keywords TEXT NOT NULL DEFAULT '',
            breaking_use_follows INTEGER NOT NULL DEFAULT 1,
            digest_frequency TEXT NOT NULL DEFAULT 'daily',
            digest_days TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT 'en',
            channel_id TEXT NOT NULL DEFAULT ''
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

        CREATE TABLE IF NOT EXISTS article_feedback (
            chat_id INTEGER NOT NULL,
            article_url TEXT NOT NULL,
            vote INTEGER NOT NULL CHECK (vote IN (1, -1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, article_url)
        );

        CREATE INDEX IF NOT EXISTS idx_article_feedback_url
        ON article_feedback (article_url);

        CREATE INDEX IF NOT EXISTS idx_article_feedback_created_at
        ON article_feedback (created_at);

        CREATE TABLE IF NOT EXISTS saved_articles (
            chat_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, url)
        );

        CREATE INDEX IF NOT EXISTS idx_saved_articles_saved_at
        ON saved_articles (saved_at);
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
                breaking_news_enabled INTEGER NOT NULL DEFAULT 0,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                daily_hour INTEGER NOT NULL DEFAULT 8,
                quiet_start_hour INTEGER,
                quiet_end_hour INTEGER,
                breaking_keywords TEXT NOT NULL DEFAULT '',
                breaking_use_follows INTEGER NOT NULL DEFAULT 1,
                digest_frequency TEXT NOT NULL DEFAULT 'daily',
                digest_days TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'en',
                channel_id TEXT NOT NULL DEFAULT ''
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

    if "timezone" not in columns:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")

    if "daily_hour" not in columns:
        conn.execute(
            "ALTER TABLE user_preferences ADD COLUMN daily_hour INTEGER NOT NULL DEFAULT 8"
        )

    if "quiet_start_hour" not in columns:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN quiet_start_hour INTEGER")

    if "quiet_end_hour" not in columns:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN quiet_end_hour INTEGER")

    if "breaking_keywords" not in columns:
        conn.execute(
            "ALTER TABLE user_preferences ADD COLUMN breaking_keywords TEXT NOT NULL DEFAULT ''"
        )

    if "breaking_use_follows" not in columns:
        conn.execute(
            """
            ALTER TABLE user_preferences
            ADD COLUMN breaking_use_follows INTEGER NOT NULL DEFAULT 1
            """
        )

    if "digest_frequency" not in columns:
        conn.execute(
            "ALTER TABLE user_preferences ADD COLUMN digest_frequency TEXT NOT NULL DEFAULT 'daily'"
        )

    if "digest_days" not in columns:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN digest_days TEXT NOT NULL DEFAULT ''")

    if "language" not in columns:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN language TEXT NOT NULL DEFAULT 'en'")

    if "channel_id" not in columns:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN channel_id TEXT NOT NULL DEFAULT ''")


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


def _migrate_article_feedback(conn: sqlite3.Connection) -> None:
    """Create article_feedback table for existing databases."""
    if not _table_exists(conn, "article_feedback"):
        conn.execute(
            """
            CREATE TABLE article_feedback (
                chat_id INTEGER NOT NULL,
                article_url TEXT NOT NULL,
                vote INTEGER NOT NULL CHECK (vote IN (1, -1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, article_url)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_article_feedback_url ON article_feedback (article_url)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_article_feedback_created_at "  # noqa: E501
            "ON article_feedback (created_at)"  # noqa: E501
        )


def _migrate_saved_articles(conn: sqlite3.Connection) -> None:
    """Create saved_articles table for existing databases."""
    if not _table_exists(conn, "saved_articles"):
        conn.execute(
            """
            CREATE TABLE saved_articles (
                chat_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, url)
            )
            """
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_saved_articles_saved_at ON saved_articles (saved_at)"
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
    _migrate_article_feedback(conn)
    _migrate_saved_articles(conn)


def _init_db() -> None:
    """Initialize database and apply schema migrations."""

    with _lock:
        conn = _get_connection()
        _create_schema(conn)
        _migrate_schema(conn)
        conn.commit()
        conn.close()


def _close_cached_connections() -> None:
    """No-op retained for test/conftest compatibility.

    Connections are created per-operation and closed by each ``_*_sync``
    helper, so there is no shared connection to close between tests.
    """


def _normalize_topic(topic: str) -> str:
    """Normalize a followed topic for dedupe and comparison."""
    return " ".join(topic.strip().lower().split())


def _normalize_alert_key(article_key: str) -> str:
    """Normalize an article identifier for breaking-alert dedupe."""
    return " ".join(article_key.strip().lower().split())


# NOTE: the database is initialized explicitly from the application startup
# path (bot/main.py calls _init_db()); importing this module stays
# side-effect-free so tooling and tests can load it without a database.


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


def _safe_str_to_int(value: object, default: int = 0) -> int:
    """Convert a DB value to int, returning ``default`` on failure."""
    if value is None:
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _default_prefs(default_country: str = "us") -> dict[str, str]:
    return {
        "country": default_country,
        "category": "general",
        "timezone": "UTC",
        "daily_hour": "8",
        "quiet_start_hour": "",
        "quiet_end_hour": "",
        "breaking_keywords": "",
        "breaking_use_follows": "1",
        "digest_frequency": "daily",
        "digest_days": "",
        "language": "en",
        "channel_id": "",
    }


VALID_DIGEST_FREQUENCIES = frozenset({"daily", "twice", "weekdays", "custom"})


def _sanitize_digest_frequency(value: object) -> str:
    raw = str(value or "daily").strip().lower()
    return raw if raw in VALID_DIGEST_FREQUENCIES else "daily"


def parse_digest_days(raw: str) -> list[int]:
    """Parse stored digest_days CSV into sorted unique weekday ints 0-6."""
    seen: set[int] = set()
    result: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            continue
        if 0 <= n <= 6 and n not in seen:
            seen.add(n)
            result.append(n)
    result.sort()
    return result


def serialize_digest_days(days: list[int]) -> str:
    cleaned = sorted({int(d) for d in days if 0 <= int(d) <= 6})
    return ",".join(str(d) for d in cleaned)


def _row_to_prefs(row: sqlite3.Row, default_country: str = "us") -> dict[str, str]:
    quiet_start = row["quiet_start_hour"]
    quiet_end = row["quiet_end_hour"]
    # Support older DBs where digest columns may not exist (pre-migration).
    try:
        df_raw = row["digest_frequency"]
    except (IndexError, KeyError):
        df_raw = "daily"
    try:
        dd_raw = row["digest_days"]
    except (IndexError, KeyError):
        dd_raw = ""
    try:
        lang_raw = row["language"]
    except (IndexError, KeyError):
        lang_raw = "en"
    try:
        ch_raw = row["channel_id"]
    except (IndexError, KeyError):
        ch_raw = ""
    return {
        "country": str(row["country"] if row["country"] is not None else default_country),
        "category": str(row["category"] if row["category"] is not None else "general"),
        "timezone": str(row["timezone"] if row["timezone"] else "UTC"),
        "daily_hour": str(_safe_str_to_int(row["daily_hour"], 8)),
        "quiet_start_hour": "" if quiet_start is None else str(_safe_str_to_int(quiet_start)),
        "quiet_end_hour": "" if quiet_end is None else str(_safe_str_to_int(quiet_end)),
        "breaking_keywords": str(row["breaking_keywords"] or ""),
        "breaking_use_follows": "1"
        if _safe_str_to_int(row["breaking_use_follows"], 1) != 0
        else "0",
        "digest_frequency": _sanitize_digest_frequency(df_raw or "daily"),
        "digest_days": str(dd_raw or ""),
        "language": str(lang_raw or "en").strip().lower() or "en",
        "channel_id": str(ch_raw or "").strip(),
    }


def _get_user_prefs_sync(chat_id: int, default_country: str = "us") -> dict[str, str]:
    with _lock:
        conn = _get_connection()
        try:
            # Prefer new schema; fall back for unmigrated DBs without new columns.
            try:
                cursor = conn.execute(
                    """
                    SELECT country, category, timezone, daily_hour,
                           quiet_start_hour, quiet_end_hour,
                           breaking_keywords, breaking_use_follows,
                           digest_frequency, digest_days, language, channel_id
                    FROM user_preferences
                    WHERE chat_id = ?
                    """,
                    (chat_id,),
                )
                row = cursor.fetchone()
            except sqlite3.OperationalError:
                try:
                    cursor = conn.execute(
                        """
                        SELECT country, category, timezone, daily_hour,
                               quiet_start_hour, quiet_end_hour,
                               breaking_keywords, breaking_use_follows,
                               digest_frequency, digest_days, language
                        FROM user_preferences
                        WHERE chat_id = ?
                        """,
                        (chat_id,),
                    )
                    row = cursor.fetchone()
                except sqlite3.OperationalError:
                    try:
                        cursor = conn.execute(
                            """
                            SELECT country, category, timezone, daily_hour,
                                   quiet_start_hour, quiet_end_hour,
                                   breaking_keywords, breaking_use_follows,
                                   digest_frequency, digest_days
                            FROM user_preferences
                            WHERE chat_id = ?
                            """,
                            (chat_id,),
                        )
                        row = cursor.fetchone()
                    except sqlite3.OperationalError:
                        cursor = conn.execute(
                            """
                            SELECT country, category, timezone, daily_hour,
                                   quiet_start_hour, quiet_end_hour,
                                   breaking_keywords, breaking_use_follows
                            FROM user_preferences
                            WHERE chat_id = ?
                            """,
                            (chat_id,),
                        )
                        row = cursor.fetchone()
            if row:
                return _row_to_prefs(row, default_country)
            return _default_prefs(default_country)
        finally:
            conn.close()


async def get_user_prefs(chat_id: int, default_country: str = "us") -> dict[str, str]:
    return await asyncio.to_thread(_get_user_prefs_sync, chat_id, default_country)


def _set_user_prefs_sync(
    chat_id: int,
    country: str | None = None,
    category: str | None = None,
    timezone: str | None = None,
    daily_hour: int | None = None,
    quiet_start_hour: int | None = None,
    quiet_end_hour: int | None = None,
    clear_quiet_hours: bool = False,
    breaking_keywords: str | None = None,
    breaking_use_follows: bool | None = None,
    digest_frequency: str | None = None,
    digest_days: str | None = None,
    language: str | None = None,
    channel_id: str | None = None,
    default_country: str = DEFAULT_COUNTRY,
) -> dict[str, str]:
    """Upsert user preferences. Only provided fields are updated."""
    with _lock:
        conn = _get_connection()
        try:
            # BEGIN IMMEDIATE prevents lost-update race: SELECT + INSERT
            # is atomic w.r.t. other writers (WAL + busy_timeout handles contention).
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    SELECT country, category, timezone, daily_hour,
                           quiet_start_hour, quiet_end_hour,
                           breaking_keywords, breaking_use_follows,
                           digest_frequency, digest_days, language
                    FROM user_preferences
                    WHERE chat_id = ?
                    """,
                    (chat_id,),
                )
                row = cursor.fetchone()
            except sqlite3.OperationalError:
                try:
                    cursor = conn.execute(
                        """
                        SELECT country, category, timezone, daily_hour,
                               quiet_start_hour, quiet_end_hour,
                               breaking_keywords, breaking_use_follows,
                               digest_frequency, digest_days
                        FROM user_preferences
                        WHERE chat_id = ?
                        """,
                        (chat_id,),
                    )
                    row = cursor.fetchone()
                except sqlite3.OperationalError:
                    cursor = conn.execute(
                        """
                        SELECT country, category, timezone, daily_hour,
                               quiet_start_hour, quiet_end_hour,
                               breaking_keywords, breaking_use_follows
                        FROM user_preferences
                        WHERE chat_id = ?
                        """,
                        (chat_id,),
                    )
                    row = cursor.fetchone()

            current = (
                _row_to_prefs(row, default_country) if row else _default_prefs(default_country)
            )

            if country is not None:
                current["country"] = country
            if category is not None:
                current["category"] = category
            if timezone is not None:
                current["timezone"] = timezone
            if daily_hour is not None:
                current["daily_hour"] = str(int(daily_hour))
            if clear_quiet_hours:
                current["quiet_start_hour"] = ""
                current["quiet_end_hour"] = ""
            else:
                if quiet_start_hour is not None:
                    current["quiet_start_hour"] = str(int(quiet_start_hour))
                if quiet_end_hour is not None:
                    current["quiet_end_hour"] = str(int(quiet_end_hour))
            if breaking_keywords is not None:
                current["breaking_keywords"] = breaking_keywords
            if breaking_use_follows is not None:
                current["breaking_use_follows"] = "1" if breaking_use_follows else "0"
            if digest_frequency is not None:
                current["digest_frequency"] = _sanitize_digest_frequency(digest_frequency)
            if digest_days is not None:
                # Store sanitized CSV (empty string for none).
                current["digest_days"] = serialize_digest_days(parse_digest_days(digest_days))
            if language is not None:
                # Normalize: lowercase, allow 'all', fallback to 'en' on empty.
                lang = str(language).strip().lower() or "en"
                current["language"] = lang
            if channel_id is not None:
                # Normalize: strip @ prefix from @channelname inputs.
                current["channel_id"] = str(channel_id).strip()

            quiet_start_db: int | None = (
                int(current["quiet_start_hour"]) if current["quiet_start_hour"] != "" else None
            )
            quiet_end_db: int | None = (
                int(current["quiet_end_hour"]) if current["quiet_end_hour"] != "" else None
            )

            conn.execute(
                """
                INSERT INTO user_preferences (
                    chat_id,
                    country,
                    category,
                    timezone,
                    daily_hour,
                    quiet_start_hour,
                    quiet_end_hour,
                    breaking_keywords,
                    breaking_use_follows,
                    digest_frequency,
                    digest_days,
                    language,
                    channel_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    country = excluded.country,
                    category = excluded.category,
                    timezone = excluded.timezone,
                    daily_hour = excluded.daily_hour,
                    quiet_start_hour = excluded.quiet_start_hour,
                    quiet_end_hour = excluded.quiet_end_hour,
                    breaking_keywords = excluded.breaking_keywords,
                    breaking_use_follows = excluded.breaking_use_follows,
                    digest_frequency = excluded.digest_frequency,
                    digest_days = excluded.digest_days,
                    language = excluded.language,
                    channel_id = excluded.channel_id
                """,
                (
                    chat_id,
                    current["country"],
                    current["category"],
                    current["timezone"],
                    int(current["daily_hour"]),
                    quiet_start_db,
                    quiet_end_db,
                    current["breaking_keywords"],
                    1 if current["breaking_use_follows"] == "1" else 0,
                    current["digest_frequency"],
                    current["digest_days"],
                    current["language"],
                    current["channel_id"],
                ),
            )
            conn.commit()
            return current
        finally:
            conn.close()


async def set_user_prefs(
    chat_id: int,
    country: str | None = None,
    category: str | None = None,
    timezone: str | None = None,
    daily_hour: int | None = None,
    quiet_start_hour: int | None = None,
    quiet_end_hour: int | None = None,
    clear_quiet_hours: bool = False,
    breaking_keywords: str | None = None,
    breaking_use_follows: bool | None = None,
    digest_frequency: str | None = None,
    digest_days: str | None = None,
    language: str | None = None,
    default_country: str = DEFAULT_COUNTRY,
) -> dict[str, str]:
    return await asyncio.to_thread(
        _set_user_prefs_sync,
        chat_id,
        country,
        category,
        timezone,
        daily_hour,
        quiet_start_hour,
        quiet_end_hour,
        clear_quiet_hours,
        breaking_keywords,
        breaking_use_follows,
        digest_frequency,
        digest_days,
        language,
        default_country,
    )


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
    retention_days = max(int(days), 1)

    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                f"""
                DELETE FROM breaking_alerts
                WHERE sent_at < datetime('now', '-{retention_days} days')
                """
            )
            conn.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0
        finally:
            conn.close()


async def cleanup_old_breaking_alerts(days: int = 14) -> int:
    return await asyncio.to_thread(_cleanup_old_breaking_alerts_sync, days)


def _count_breaking_alerts_today_sync(chat_id: int) -> int:
    """Count breaking alerts delivered to a user since UTC midnight."""
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM breaking_alerts
                WHERE chat_id = ?
                  AND sent_at >= datetime('now', 'start of day')
                """,
                (chat_id,),
            )
            row = cursor.fetchone()
            return int(row["count"]) if row else 0
        finally:
            conn.close()


async def count_breaking_alerts_today(chat_id: int) -> int:
    return await asyncio.to_thread(_count_breaking_alerts_today_sync, chat_id)


def parse_breaking_keywords(raw: str) -> list[str]:
    """Split stored keyword string into cleaned unique keywords."""
    seen: set[str] = set()
    result: list[str] = []
    for part in raw.replace(";", ",").split(","):
        cleaned = " ".join(part.strip().split())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def serialize_breaking_keywords(keywords: list[str]) -> str:
    return ", ".join(keywords)


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


def _claim_breaking_alert_slot_sync(
    chat_id: int,
    article_key: str,
    article_url: str,
    article_title: str,
    max_per_day: int,
) -> bool:
    """Atomically claim a breaking-alert send slot for a user.

    Combines the per-user daily cap (counting today's rows) with the
    (chat_id, article_key) dedupe insert in a single transaction so the
    BREAKING_ALERT_MAX_PER_DAY limit cannot be raced by concurrent workers.

    Returns True when the slot is claimed (insert succeeded and the user
    is still under the daily cap); False when the article was already sent,
    the key is invalid, or the user has hit the daily cap.
    """
    normalized_key = _normalize_alert_key(article_key)
    if not normalized_key:
        return False

    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM breaking_alerts
                WHERE chat_id = ?
                  AND sent_at >= datetime('now', 'start of day')
                """,
                (chat_id,),
            )
            row = cursor.fetchone()
            today_count = int(row["count"]) if row else 0
            if today_count >= max_per_day:
                return False

            insert_cursor = conn.execute(
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
            return insert_cursor.rowcount > 0
        finally:
            conn.close()


async def claim_breaking_alert_slot(
    chat_id: int,
    article_key: str,
    article_url: str = "",
    article_title: str = "",
    max_per_day: int = 10,
) -> bool:
    return await asyncio.to_thread(
        _claim_breaking_alert_slot_sync,
        chat_id,
        article_key,
        article_url,
        article_title,
        max_per_day,
    )


# ── Bookmarks ────────────────────────────────────────────────────────


def _save_article_sync(chat_id: int, url: str, title: str = "") -> bool:
    """Save an article. Returns True if newly saved, False if already exists or invalid."""
    url = url.strip()
    title = (title or "").strip()
    if not url:
        return False
    # Basic URL validation: must be http/https with netloc.
    try:
        from urllib.parse import urlparse as _urlparse

        parsed = _urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
    except Exception:
        return False
    if not title:
        title = url
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO saved_articles (chat_id, url, title)
                VALUES (?, ?, ?)
                """,
                (chat_id, url, title),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


async def save_article(chat_id: int, url: str, title: str = "") -> bool:
    return await asyncio.to_thread(_save_article_sync, chat_id, url, title)


def _unsave_article_sync(chat_id: int, url: str) -> bool:
    """Remove a saved article. Returns True if removed."""
    url = url.strip()
    if not url:
        return False
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM saved_articles WHERE chat_id = ? AND url = ?",
                (chat_id, url),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


async def unsave_article(chat_id: int, url: str) -> bool:
    return await asyncio.to_thread(_unsave_article_sync, chat_id, url)


def _list_saved_sync(chat_id: int) -> list[dict[str, str]]:
    """Return saved articles ordered by most recent first."""
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT url, title, saved_at
                FROM saved_articles
                WHERE chat_id = ?
                ORDER BY saved_at DESC, rowid DESC
                """,
                (chat_id,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "url": str(r["url"]),
                    "title": str(r["title"] or r["url"]),
                    "saved_at": str(r["saved_at"]),
                }
                for r in rows
            ]
        finally:
            conn.close()


async def list_saved(chat_id: int) -> list[dict[str, str]]:
    return await asyncio.to_thread(_list_saved_sync, chat_id)


def _is_saved_sync(chat_id: int, url: str) -> bool:
    url = url.strip()
    if not url:
        return False
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                "SELECT 1 FROM saved_articles WHERE chat_id = ? AND url = ?",
                (chat_id, url),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()


async def is_saved(chat_id: int, url: str) -> bool:
    return await asyncio.to_thread(_is_saved_sync, chat_id, url)


def _clear_saved_sync(chat_id: int) -> int:
    """Remove all saved articles for a user. Returns number removed."""
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM saved_articles WHERE chat_id = ?",
                (chat_id,),
            )
            conn.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0
        finally:
            conn.close()


async def clear_saved(chat_id: int) -> int:
    return await asyncio.to_thread(_clear_saved_sync, chat_id)
