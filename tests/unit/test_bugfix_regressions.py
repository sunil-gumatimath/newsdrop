"""Regression tests for bugs found in the end-to-end feature review.

Each test pins one defect that shipped: the schema-migration short circuit,
naive HTML truncation in the search callback, colon-bearing topics in callback
payloads, the ``\\b`` keyword boundary in breaking alerts, and a few smaller
correctness issues in jobs / export / shutdown.

Fakes follow the MagicMock/AsyncMock style already used in tests/unit.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery, InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from newsdrop import database as db
from newsdrop import news_fetcher as nf
from newsdrop.bot import commands, helpers, jobs
from newsdrop.bot.callbacks import _extract_ownership_user_id, _handle_search_callback
from newsdrop.message_utils import chunk_message

# ── 1. Schema migration must actually add missing columns ──────────────


def _legacy_db(path: Path) -> None:
    """Create a pre-``digest_frequency`` database with one user row."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
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
            breaking_use_follows INTEGER NOT NULL DEFAULT 1
        )
    """)
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
    conn.execute(
        """
        CREATE TABLE breaking_alerts (
            chat_id INTEGER NOT NULL,
            article_key TEXT NOT NULL,
            article_url TEXT NOT NULL DEFAULT '',
            article_title TEXT NOT NULL DEFAULT '',
            sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, article_key)
        )
        """
    )
    conn.execute("INSERT INTO user_preferences (chat_id, country) VALUES (4242, 'gb')")
    conn.commit()
    conn.close()


def _legacy_db_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """Point the database module at a legacy-schema file and migrate it."""
    db_file = tmp_path / name
    _legacy_db(db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    db._init_db()
    return db_file


def test_migration_adds_missing_columns_to_legacy_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy DB must gain digest_frequency/digest_days and keep its rows."""
    db_file = _legacy_db_at(tmp_path, monkeypatch, "legacy.db")

    check = sqlite3.connect(str(db_file))
    cols = {r[1] for r in check.execute("PRAGMA table_info(user_preferences)")}
    assert "digest_frequency" in cols, "migration did not add digest_frequency"
    assert "digest_days" in cols, "migration did not add digest_days"

    rows = check.execute("SELECT chat_id, digest_frequency FROM user_preferences").fetchall()
    check.close()
    assert rows == [(4242, "daily")], "existing rows must survive the migration"


async def test_set_prefs_works_on_legacy_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime failure: writing prefs to a legacy DB raised OperationalError."""
    _legacy_db_at(tmp_path, monkeypatch, "legacy2.db")

    await db.set_user_prefs(999, digest_frequency="twice", digest_days="1,3,5")
    prefs = await db.get_user_prefs(999)
    assert prefs["digest_frequency"] == "twice"
    assert db.parse_digest_days(prefs["digest_days"]) == [1, 3, 5]


def test_migration_fresh_db_keeps_schema(tmp_db: str) -> None:
    """Sanity check: the fix did not break the fresh-install path."""
    conn = db._get_connection()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_preferences)")}
    conn.close()
    assert {"digest_frequency", "digest_days"} <= cols


# ── 2. Search callback must not truncate raw HTML ──────────────────────

_LONG_TITLE = ("Corporate funding deal " + "market commentary " * 25).strip()


def _long_articles() -> list[dict[str, Any]]:
    """Eight long-titled results — enough to blow past Telegram's 4096 limit."""
    return [
        {
            "title": _LONG_TITLE,
            "url": f"https://example.com/story/{i}",
            "description": "d" * 200,
            "source": {"name": "Reuters"},
            "publishedAt": "2026-10-06T10:00:00+00:00",
        }
        for i in range(8)
    ]


def _long_search_digest() -> str:
    """Build a search digest that exceeds the single-message limit."""
    digest = helpers.build_search_payload({"articles": _long_articles()}, "funding").digest
    assert digest is not None, "fixture must produce a digest"
    assert len(digest) > 4096, "fixture must exceed the Telegram limit"
    return cast(str, digest)


def test_naive_cut_breaks_html_but_chunker_does_not() -> None:
    """Guards the regression: a raw 3990-char cut leaves ``<b>`` unclosed."""
    digest = _long_search_digest()

    naive = digest[:3990] + "…"
    assert naive.count("<b>") != naive.count("</b>"), (
        "expected the naive cut to unbalance HTML (this is the bug)"
    )

    for chunk in chunk_message(digest):
        assert len(chunk) <= 4096
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<i>") == chunk.count("</i>")


async def test_search_callback_chunks_long_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callback path must chunk instead of cutting inside an HTML tag."""
    sent: list[str] = []

    # send_chunked_message() replies off the status placeholder, so record
    # both the placeholder reply and the chunk replies on this one mock.
    status_msg = MagicMock()
    status_msg.delete = AsyncMock(return_value=None)
    status_msg.edit_text = AsyncMock(return_value=None)

    async def record_reply(text: str, *_args: Any, **_kwargs: Any) -> MagicMock:
        sent.append(text)
        return cast(MagicMock, status_msg)

    status_msg.reply_text = AsyncMock(side_effect=record_reply)

    query = MagicMock()
    query.answer = AsyncMock(return_value=None)
    query.edit_message_text = AsyncMock(return_value=None)
    query.message = MagicMock()
    query.message.reply_text = AsyncMock(return_value=status_msg)

    async def fake_search(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"articles": _long_articles(), "totalResults": 8}

    async def fake_prefs(_chat_id: int, _default: str | None = None) -> dict[str, str]:
        return {"country": "us"}

    async def allow(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("newsdrop.bot.callbacks.search_news", fake_search)
    monkeypatch.setattr("newsdrop.bot.callbacks.get_user_prefs", fake_prefs)
    monkeypatch.setattr("newsdrop.bot.callbacks.rate_limit_try_acquire", allow)

    await _handle_search_callback(cast(CallbackQuery, query), 1, "funding")

    status_msg.delete.assert_awaited_once()
    assert len(sent) >= 2, "long results must be split across messages"
    for text in sent:
        assert text.count("<b>") == text.count("</b>")
        assert len(text) <= 4096


# ── 3. Colon-bearing topics must survive callback round-trip ───────────


@pytest.mark.parametrize(
    "payload",
    ["follow:covid%3A19", "unfollow:covid%3A19", "follow:ratio%3A3", "search:covid%3A19"],
)
def test_encoded_topics_are_not_mistaken_for_user_id(payload: str) -> None:
    """Encoded topics must never be parsed as a trailing ``:<user_id>``."""
    action = payload.split(":", 1)[0]

    update = MagicMock()
    update.callback_query.data = payload

    assert _extract_ownership_user_id(cast(Update, update), action) is None


async def test_follow_callback_round_trips_encoded_topic(tmp_db: str) -> None:
    """Following ``#covid:19`` must store the full topic, not ``covid``."""
    encoded = helpers._encode_callback_value("covid:19")
    assert encoded == "covid%3A19"

    created, stored = await db.add_followed_topic(555, helpers._decode_callback_value(encoded))
    assert created is True
    assert stored == "covid:19"


def test_digest_keyboard_encodes_follow_topic() -> None:
    """Follow buttons must encode so ``:`` cannot fake an ownership segment."""
    markup = helpers._build_digest_keyboard([], follow_topic="covid:19")
    assert markup is not None

    button = cast(InlineKeyboardButton, markup.inline_keyboard[0][0])
    data = cast(str, button.callback_data)
    assert data == "follow:covid%3A19"
    assert data.split(":")[-1] != "19"


async def test_trending_rows_encode_topics(tmp_db: str) -> None:
    """_build_trending_topic_rows keeps colon topics in a single segment."""
    rows = await helpers._build_trending_topic_rows(777, ["covid:19", "ai"])

    payloads: list[str] = []
    for row in rows:
        for btn in row:
            payloads.append(cast(str, btn.callback_data))

    assert "search:covid%3A19" in payloads
    assert any(p.startswith("follow:covid%3A19") for p in payloads)
    assert all(len(p.encode("utf-8")) <= 64 for p in payloads)


# ── 4. Breaking-alert reason uses whole-token matching ─────────────────


def test_breaking_alert_reason_includes_non_word_topics() -> None:
    """``c++`` must appear in the matched reason (``\\b`` dropped it)."""
    article = {
        "title": "AI tooling ships with c++ interop improvements",
        "description": "The compiler update landed today.",
        "url": "https://example.com/x",
    }
    text, _ = helpers.format_breaking_alert(article, ["ai", "c++"], used_today=1, max_per_day=5)
    reason = text.splitlines()[1]
    assert "c++" in reason, f"matched topic missing from reason line: {reason!r}"


def test_breaking_alert_title_hits_avoid_false_positives() -> None:
    """Token matching must not treat ``ai`` as a hit inside ``airport``."""
    airport = {"title": "Airport expansion continues", "description": "Travel update."}
    assert jobs.matching_alert_keywords(airport, ["ai"]) == []

    ai_rules = {"title": "AI rules published", "description": ""}
    assert jobs.matching_alert_keywords(ai_rules, ["ai"]) == ["ai"]


# ── 5. Smaller correctness fixes ───────────────────────────────────────


async def test_export_caption_reports_byte_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caption must report bytes written, not characters in the string."""
    captured: dict[str, Any] = {}

    async def fake_top_headlines(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"articles": [], "sources": []}

    async def fake_prefs(_chat_id: int, _d: str | None = None) -> dict[str, str]:
        return {"country": "jp", "category": "technology"}

    async def fake_followed(_chat_id: int) -> list[str]:
        return []

    status_msg = MagicMock()
    status_msg.delete = AsyncMock(return_value=None)
    status_msg.edit_text = AsyncMock(return_value=None)

    message = MagicMock()
    message.message_id = 7
    message.reply_text = AsyncMock(return_value=status_msg)
    message.reply_document = AsyncMock(side_effect=lambda **kw: captured.update(kw))

    update = MagicMock()
    update.effective_message = message
    update.effective_chat = MagicMock(id=1)
    update.effective_user = MagicMock(id=1)

    monkeypatch.setattr("newsdrop.bot.commands.fetch_top_headlines", fake_top_headlines)
    monkeypatch.setattr("newsdrop.bot.commands.get_user_prefs", fake_prefs)
    monkeypatch.setattr("newsdrop.bot.commands.get_followed_topics", fake_followed)

    await commands.export_briefing(cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, None))

    payload = cast(bytes, cast(Any, captured["document"]).getvalue())
    assert f"{len(payload)} bytes HTML" in cast(str, captured["caption"])


def test_http_client_lock_is_per_event_loop() -> None:
    """Closing the client from a second loop must not raise."""

    async def make() -> asyncio.Lock:
        # _http_client_lock() is sync and binds the lock to the *running* loop.
        return cast(asyncio.Lock, nf._http_client_lock())

    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        lock_a = loop_a.run_until_complete(make())
        lock_b = loop_b.run_until_complete(make())
        assert lock_a is not lock_b
    finally:
        loop_a.close()
        loop_b.close()


async def test_claim_slot_respects_cap_and_dedupes(tmp_db: str) -> None:
    """The cap gate and dedupe insert must behave as one unit."""
    for i in range(2):
        assert await db.claim_breaking_alert_slot(1, f"key-{i}", "u", "t", max_per_day=2) is True

    assert await db.claim_breaking_alert_slot(1, "key-3", "u", "t", max_per_day=2) is False
    assert await db.count_breaking_alerts_today(1) == 2
    # Re-claiming an already-sent article must not consume another slot.
    assert await db.claim_breaking_alert_slot(1, "key-0", "u", "t", max_per_day=5) is False


async def test_send_daily_news_groups_in_one_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_db: str
) -> None:
    """Prefs are read once per user, and grouping comes from that same read."""
    calls: list[int] = []

    async def fake_prefs(chat_id: int, _default: str | None = None) -> dict[str, str]:
        calls.append(chat_id)
        return {"country": "us", "category": "general", "daily_hour": "8", "timezone": "UTC"}

    async def fake_all_users() -> set[int]:
        return {1, 2, 3}

    grouped: dict[Any, Any] = {}

    async def fake_combo(_context: Any, g: dict[Any, Any], _sem: Any) -> None:
        grouped.update(g)

    monkeypatch.setattr("newsdrop.bot.jobs.get_user_prefs", fake_prefs)
    monkeypatch.setattr("newsdrop.bot.jobs.load_all_user_ids", fake_all_users)
    monkeypatch.setattr("newsdrop.bot.jobs._send_combo", fake_combo)
    monkeypatch.setattr(jobs, "is_digest_due", lambda _prefs, _now=None: True)

    await jobs.send_daily_news(cast(ContextTypes.DEFAULT_TYPE, None))

    assert sorted(calls) == [1, 2, 3], "prefs must be read exactly once per user"
    assert grouped == {("us", "general"): [1, 2, 3]}
