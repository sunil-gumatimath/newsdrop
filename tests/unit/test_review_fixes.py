"""Regression tests for the end-to-end review fixes.

Each test pins one behaviour that was wrong (or fragile) before the fix.
"""

from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

# ── 1. search callback must always answer the CallbackQuery ──────────────


def _search_update(topic="bitcoin"):
    answered: list[tuple] = []

    async def _noop(*_args, **_kwargs):
        return None

    class Msg:
        """Stand-in for the Telegram Message the status reply is sent to.

        ``reply_text`` must return a *message object* with ``edit_text`` /
        ``delete``; returning None made the handler crash on its own error
        path, which is unrelated to what these tests assert.
        """

        delete = _noop

        def __getattr__(self, _name):
            return _noop

        async def reply_text(self, *_args, **_kwargs):
            return Msg()

        async def edit_text(self, *_args, **_kwargs):
            return Msg()

    class Query:
        data = f"search:{topic}"
        from_user = types.SimpleNamespace(id=1)
        message = Msg()
        edit_message_text = _noop

        async def answer(self, *a, **k):
            answered.append((a, k))

    return types.SimpleNamespace(
        callback_query=Query(), effective_chat=types.SimpleNamespace(id=1)
    ), answered


@pytest.mark.asyncio
async def test_search_callback_answers_query_on_success(tmp_db):
    """A successful search tap must answer the query.

    Previously only the rate-limit branch answered, so the client's button
    spinner kept running until it timed out on every successful search.
    """
    from newsdrop.bot import callbacks

    update, answered = _search_update()

    async def fake_search(_topic, _country):
        return {"articles": [], "totalResults": 0}

    with (
        patch.object(callbacks, "search_news", fake_search),
        patch.object(callbacks, "rate_limit_try_acquire", AsyncMock(return_value=True)),
    ):
        await callbacks.button_handler(update, types.SimpleNamespace(bot=None))

    assert answered, "search callback never answered the query"


@pytest.mark.asyncio
async def test_search_callback_answers_exactly_once_on_success(tmp_db):
    """Exactly one answer() per query — Telegram rejects a second."""
    from newsdrop.bot import callbacks

    update, answered = _search_update()

    async def fake_search(_topic, _country):
        return {"articles": [], "totalResults": 0}

    with (
        patch.object(callbacks, "search_news", fake_search),
        patch.object(callbacks, "rate_limit_try_acquire", AsyncMock(return_value=True)),
    ):
        await callbacks.button_handler(update, types.SimpleNamespace(bot=None))

    assert len(answered) == 1, f"expected 1 answer, got {len(answered)}"


@pytest.mark.asyncio
async def test_search_callback_rate_limit_answers_with_alert(tmp_db):
    """The cooldown path still answers with a visible toast, and only once."""
    from newsdrop.bot import callbacks

    update, answered = _search_update()

    with patch.object(callbacks, "rate_limit_try_acquire", AsyncMock(return_value=False)):
        await callbacks.button_handler(update, types.SimpleNamespace(bot=None))

    assert len(answered) == 1
    assert answered[0][1].get("show_alert") is True


# ── 2. env flag parsing ──────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["FALSE", "False", "no", "NO", "off", "0", " Off "])
def test_env_flag_falsy_values(raw, monkeypatch):
    from newsdrop.config import _env_flag

    monkeypatch.setenv("_TEST_FLAG", raw)
    assert _env_flag("_TEST_FLAG") is False


@pytest.mark.parametrize("raw", ["true", "TRUE", "yes", "1", "on", "enabled"])
def test_env_flag_truthy_values(raw, monkeypatch):
    from newsdrop.config import _env_flag

    monkeypatch.setenv("_TEST_FLAG", raw)
    assert _env_flag("_TEST_FLAG") is True


def test_env_flag_defaults_to_enabled_when_unset(monkeypatch):
    from newsdrop.config import _env_flag

    monkeypatch.delenv("_TEST_FLAG", raising=False)
    assert _env_flag("_TEST_FLAG") is True


def test_env_flag_blank_value_falls_back_to_default(monkeypatch):
    from newsdrop.config import _env_flag

    monkeypatch.setenv("_TEST_FLAG", "   ")
    assert _env_flag("_TEST_FLAG") is True
    assert _env_flag("_TEST_FLAG", default="0") is False


# ── 3. chunk_message tag safety ─────────────────────────────────────────


def test_chunk_never_starts_mid_tag():
    """No chunk may begin in the middle of an opening tag.

    The old 500-char lookback missed a long <a href="...">, producing a chunk
    that was literally '<a ' — which Telegram rejects outright.
    """
    import re

    from newsdrop.message_utils import MAX_MESSAGE_LENGTH, chunk_message

    text = '<a href="https://example.com/very-long-article-url">' + "w" * 5000 + "</a>"
    for chunk in chunk_message(text):
        assert len(chunk) <= MAX_MESSAGE_LENGTH
        assert not re.search(r"<a\s+[a-z]+=\"[^\">]*$", chunk), repr(chunk[:60])


def test_chunk_reopens_link_with_href_preserved():
    """A link split across chunks must keep its href, not become a bare <a>."""
    from newsdrop.message_utils import chunk_message

    url = "https://example.com/very-long-article-url"
    chunks = chunk_message(f'<a href="{url}">' + "w" * 5000 + "</a>")
    assert len(chunks) > 1
    assert any(f'href="{url}"' in c for c in chunks[1:]), "href dropped on continuation chunk"


@pytest.mark.parametrize(
    "text",
    [
        "X" * 10000,
        ("word " * 3000).strip(),
        "<b><i>" + ("deep text " * 2000) + "</i></b>",
        '<a href="https://e.com/x">' + "t" * 8000 + "</a>",
        "<b>News Briefing</b>\n\n"
        + "\n".join(
            f'<b>{i}.</b> <a href="https://example.com/a/{i}">Headline {i} body</a>\n'
            f"<i>{'z' * 700}</i>\n"
            for i in range(1, 9)
        ),
        "".join(f'<a href="https://e.com/{i}">item {i} text</a> ' for i in range(500)),
    ],
)
def test_chunk_respects_limit_and_terminates(text):
    from newsdrop.message_utils import MAX_MESSAGE_LENGTH, chunk_message

    chunks = chunk_message(text)
    assert chunks
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_LENGTH


def test_chunk_all_chunks_balanced_for_carried_tags():
    """Every chunk except the last must have balanced <b> tags."""
    from newsdrop.message_utils import chunk_message

    chunks = chunk_message("<b><i>" + ("deep text " * 2000) + "</i></b>")
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<i>") == chunk.count("</i>")


def test_chunk_fuzz_never_exceeds_limit():
    """Randomised markup/text mixes must always terminate within the limit."""
    import random

    from newsdrop.message_utils import MAX_MESSAGE_LENGTH, chunk_message

    random.seed(7)
    frags = [
        "<b>",
        "</b>",
        "<i>",
        "</i>",
        '<a href="https://e.com/x">',
        "</a>",
        "\n\n",
        "word ",
        "text ",
        "x" * 90,
        "\n",
        " ",
    ]
    for _ in range(200):
        text = "".join(random.choice(frags) for _ in range(random.randint(5, 260)))
        for chunk in chunk_message(text):
            assert len(chunk) <= MAX_MESSAGE_LENGTH


# ── 4. daily digest dedupe ledger ───────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_daily_digest_slot_is_once_per_day(tmp_db):
    from newsdrop import database

    assert await database.claim_daily_digest_slot(1, "2026-10-09") is True
    assert await database.claim_daily_digest_slot(1, "2026-10-09") is False
    # Next local day is a fresh slot.
    assert await database.claim_daily_digest_slot(1, "2026-10-10") is True


@pytest.mark.asyncio
async def test_claim_daily_digest_slot_normalizes_and_rejects_blank(tmp_db):
    from newsdrop import database

    assert await database.claim_daily_digest_slot(2, "  2026-10-09  ") is True
    assert await database.claim_daily_digest_slot(2, "2026-10-09") is False
    assert await database.claim_daily_digest_slot(3, "   ") is False


@pytest.mark.asyncio
async def test_claim_daily_digest_slot_scoped_per_chat(tmp_db):
    from newsdrop import database

    assert await database.claim_daily_digest_slot(1, "d") is True
    assert await database.claim_daily_digest_slot(2, "d") is True


@pytest.mark.asyncio
async def test_cleanup_old_daily_digests(tmp_db):
    from newsdrop import database

    await database.claim_daily_digest_slot(1, "today")
    assert await database.cleanup_old_daily_digests(days=14) == 0
    assert await database.claim_daily_digest_slot(1, "today") is False


def test_digest_dedupe_key_uses_local_date():
    from newsdrop.bot.jobs import digest_dedupe_key

    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert digest_dedupe_key({"timezone": "UTC"}, now) == "2026-10-09"


def test_digest_dedupe_key_twice_has_two_slots():
    from newsdrop.bot.jobs import digest_dedupe_key

    prefs = {"timezone": "UTC", "digest_frequency": "twice"}
    am = digest_dedupe_key(prefs, datetime(2026, 10, 9, 8, 0, tzinfo=UTC))
    pm = digest_dedupe_key(prefs, datetime(2026, 10, 9, 20, 0, tzinfo=UTC))
    assert am != pm
    assert am.endswith(":am") and pm.endswith(":pm")


def test_digest_dedupe_key_uses_user_timezone():
    from newsdrop.bot.jobs import digest_dedupe_key

    # 02:00 UTC is still the previous day in New York.
    now = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)
    assert digest_dedupe_key({"timezone": "America/New_York"}, now) == "2026-10-08"
    assert digest_dedupe_key({"timezone": "UTC"}, now) == "2026-10-09"


@pytest.mark.asyncio
async def test_send_daily_news_skips_already_delivered_user(tmp_db):
    """A second tick in the same local hour must not re-send the digest."""
    from newsdrop import database
    from newsdrop.bot import jobs

    context = types.SimpleNamespace(
        bot=types.SimpleNamespace(send_message=AsyncMock()),
    )
    prefs = {
        "country": "us",
        "category": "general",
        "timezone": "UTC",
        "daily_hour": "8",
        "digest_frequency": "daily",
        "digest_days": "",
    }
    # 08:00 UTC — the user is due for their 08:00 digest.
    frozen_now = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)

    with (
        patch.object(jobs, "load_all_user_ids", AsyncMock(return_value={7})),
        patch.object(jobs, "get_user_prefs", AsyncMock(return_value=prefs)),
        patch.object(jobs, "get_followed_topics", AsyncMock(return_value=[])),
        patch.object(jobs, "fetch_top_headlines", AsyncMock(return_value={})),
        patch.object(jobs, "cleanup_old_daily_digests", AsyncMock(return_value=0)),
        patch("newsdrop.bot.jobs.datetime") as mock_dt,
    ):
        mock_dt.now.return_value = frozen_now
        await jobs.send_daily_news(context)
        first_count = context.bot.send_message.await_count

        # Second tick, same hour: the ledger must suppress it.
        await jobs.send_daily_news(context)
        second_count = context.bot.send_message.await_count

    assert second_count == first_count, "digest was sent twice in one local hour"
    # And the ledger really recorded the delivery.
    assert await database.claim_daily_digest_slot(7, "2026-10-09") is False


# ── 5. API budget reservation is atomic ─────────────────────────────────


def test_reserve_api_slot_raises_when_exhausted():
    """Exhausting the budget must raise from the single atomic consume call."""
    from newsdrop import news_fetcher
    from newsdrop.state import api_request_consume

    async def _run():
        limit = news_fetcher._request_limit
        for _ in range(limit):
            await api_request_consume(limit)
        with pytest.raises(news_fetcher.APIClientError) as exc:
            await news_fetcher._reserve_api_slot()
        assert exc.value.status_code == 429

    asyncio.run(_run())


def test_reserve_api_slot_allows_within_budget():
    from newsdrop import news_fetcher

    async def _run():
        await news_fetcher._reserve_api_slot()

    asyncio.run(_run())


def test_reserve_api_slot_noop_when_unlimited(monkeypatch):
    """DAILY_REQUEST_LIMIT=0 means unlimited and must never raise."""
    from newsdrop import news_fetcher

    monkeypatch.setattr(news_fetcher, "_request_limit", 0)

    async def _run():
        for _ in range(50):
            await news_fetcher._reserve_api_slot()

    asyncio.run(_run())


# ── 6. helpers / state consistency ──────────────────────────────────────


def test_get_source_name_handles_bare_string_source():
    """A string source must not silently render as 'Unknown'."""
    from newsdrop.bot.helpers import _get_source_name

    assert _get_source_name({"source": "Reuters"}) == ("Reuters", "Reuters")


def test_memory_backend_records_no_state_at_zero_cooldown():
    """A 0s cooldown must not accumulate one entry per chat."""
    from newsdrop.state import _MemoryBackend

    async def _run():
        backend = _MemoryBackend()
        for _ in range(5):
            assert await backend.try_acquire_rate_limit("news", 1, 0) is True
        assert backend._rate_limits == {}

        limited = _MemoryBackend()
        assert await limited.try_acquire_rate_limit("news", 1, 60) is True
        assert await limited.try_acquire_rate_limit("news", 1, 60) is False

    asyncio.run(_run())
