"""Tests for bot/jobs.py — scheduled daily news and breaking news alert jobs.

All external dependencies (NewsData.io API, Telegram bot, Redis metrics) are mocked
so the tests run fast and deterministic. The ``tmp_db`` fixture from ``conftest.py``
provides a fresh SQLite database for each test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from newsdrop.bot import jobs

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_context() -> MagicMock:
    """Build a minimal Telegram ContextTypes mock with an async bot."""
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock(return_value=None)
    context.bot.send_photo = AsyncMock(return_value=None)
    return context


def _make_articles(n: int = 3, prefix: str = "article", country: str = "us") -> list[dict]:
    """Return a list of n fake NewsAPI-style article dicts."""
    return [
        {
            "title": f"{prefix} {i}",
            "description": f"Description for {prefix} {i}.",
            "url": f"https://example.com/{prefix}/{i}",
            "urlToImage": "",
            "publishedAt": f"2025-01-0{i + 1}T00:00:00Z",
            "source": {"name": "TestSource"},
            "country": country,
        }
        for i in range(n)
    ]


# ── send_daily_news ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_daily_news_no_subscribers(tmp_db):
    """When no subscribers exist, the job exits early without API calls."""
    context = _make_context()

    with patch("newsdrop.bot.jobs.load_subscribers", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = set()
        await jobs.send_daily_news(context)

    # No messages should have been sent
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_daily_news_groups_by_country_category(tmp_db):
    """Subscribers with the same (country, category) are grouped; one fetch per combo."""
    context = _make_context()

    subscribers = {100, 101, 102, 200}

    # 100, 101 → us/general; 102 → us/technology; 200 → gb/general
    prefs_map = {
        100: {"country": "us", "category": "general"},
        101: {"country": "us", "category": "general"},
        102: {"country": "us", "category": "technology"},
        200: {"country": "gb", "category": "general"},
    }

    async def fake_get_prefs(chat_id, default_country="us"):
        return prefs_map.get(chat_id, {"country": default_country, "category": "general"})

    fake_articles = {
        "status": "ok",
        "totalResults": 2,
        "articles": _make_articles(2),
    }

    with (
        patch("newsdrop.bot.jobs.load_subscribers", new_callable=AsyncMock) as mock_load,
        patch("newsdrop.bot.jobs.get_user_prefs", new_callable=AsyncMock) as mock_prefs,
        patch("newsdrop.bot.jobs.get_followed_topics", new_callable=AsyncMock, return_value=[]),
        patch("newsdrop.bot.jobs.fetch_top_headlines", new_callable=AsyncMock) as mock_fetch,
        patch("newsdrop.bot.jobs.increment", new_callable=AsyncMock),
        patch("newsdrop.bot.jobs.is_digest_due", return_value=True),
    ):
        mock_load.return_value = subscribers
        mock_prefs.side_effect = fake_get_prefs
        mock_fetch.return_value = fake_articles

        await jobs.send_daily_news(context)

    # Should have fetched exactly 3 unique combos: (us,general), (us,technology), (gb,general)
    assert mock_fetch.call_count == 3
    fetched_combos = {call.args for call in mock_fetch.call_args_list}
    assert fetched_combos == {
        ("us", "general"),
        ("us", "technology"),
        ("gb", "general"),
    }

    # 4 subscribers each get a message
    assert context.bot.send_message.call_count == 4


@pytest.mark.asyncio
async def test_send_daily_news_chunks_long_digest(tmp_db):
    """Digests exceeding 4096 chars are split into multiple messages."""
    context = _make_context()

    # Build articles with very long titles+descriptions to force chunking past 4096
    long_articles = [
        {
            "title": f"Long Article Headline Number {i} " * 20,
            "description": "x" * 800,
            "url": f"https://example.com/long/{i}",
            "urlToImage": "",
            "publishedAt": "2025-01-01T00:00:00Z",
            "source": {"name": "LongSource"},
        }
        for i in range(10)
    ]

    fake_articles = {
        "status": "ok",
        "totalResults": 10,
        "articles": long_articles,
    }

    async def fake_get_prefs(chat_id, default_country="us"):
        return {"country": "us", "category": "general"}

    with (
        patch("newsdrop.bot.jobs.load_subscribers", new_callable=AsyncMock) as mock_load,
        patch("newsdrop.bot.jobs.get_user_prefs", new_callable=AsyncMock) as mock_prefs,
        patch("newsdrop.bot.jobs.get_followed_topics", new_callable=AsyncMock, return_value=[]),
        patch("newsdrop.bot.jobs.fetch_top_headlines", new_callable=AsyncMock) as mock_fetch,
        patch("newsdrop.bot.jobs.increment", new_callable=AsyncMock),
        patch("newsdrop.bot.jobs.is_digest_due", return_value=True),
    ):
        mock_load.return_value = {100}
        mock_prefs.side_effect = fake_get_prefs
        mock_fetch.return_value = fake_articles

        await jobs.send_daily_news(context)

    # Should have sent more than 1 message (digest was chunked)
    assert context.bot.send_message.call_count > 1


@pytest.mark.asyncio
async def test_send_daily_news_handles_api_error(tmp_db):
    """When the API returns an error, subscribers get a fallback message."""
    from newsdrop.news_fetcher import APIClientError

    context = _make_context()

    async def fake_get_prefs(chat_id, default_country="us"):
        return {"country": "us", "category": "general"}

    with (
        patch("newsdrop.bot.jobs.load_subscribers", new_callable=AsyncMock) as mock_load,
        patch("newsdrop.bot.jobs.get_user_prefs", new_callable=AsyncMock) as mock_prefs,
        patch("newsdrop.bot.jobs.fetch_top_headlines", new_callable=AsyncMock) as mock_fetch,
        patch("newsdrop.bot.jobs.is_digest_due", return_value=True),
    ):
        mock_load.return_value = {100, 101}
        mock_prefs.side_effect = fake_get_prefs
        mock_fetch.side_effect = APIClientError("API down", status_code=500)

        await jobs.send_daily_news(context)

    # Each subscriber should receive a fallback "could not fetch" message
    assert context.bot.send_message.call_count == 2
    for call in context.bot.send_message.call_args_list:
        text = call.kwargs.get("text", call.args[0] if call.args else "")
        assert "could not" in text.lower() or "try again" in text.lower()


@pytest.mark.asyncio
async def test_send_daily_news_empty_results_sends_no_articles_message(tmp_db):
    """When the API returns zero articles, subscribers see the empty-state hint."""
    context = _make_context()

    empty_articles = {
        "status": "ok",
        "totalResults": 0,
        "articles": [],
        "sources": ["newsdata.io"],
    }

    async def fake_get_prefs(chat_id, default_country="us"):
        return {"country": "us", "category": "general"}

    with (
        patch("newsdrop.bot.jobs.load_subscribers", new_callable=AsyncMock) as mock_load,
        patch("newsdrop.bot.jobs.get_user_prefs", new_callable=AsyncMock) as mock_prefs,
        patch("newsdrop.bot.jobs.get_followed_topics", new_callable=AsyncMock, return_value=[]),
        patch("newsdrop.bot.jobs.fetch_top_headlines", new_callable=AsyncMock) as mock_fetch,
        patch("newsdrop.bot.jobs.is_digest_due", return_value=True),
    ):
        mock_load.return_value = {100}
        mock_prefs.side_effect = fake_get_prefs
        mock_fetch.return_value = empty_articles

        await jobs.send_daily_news(context)

    # Should send the empty-state message
    assert context.bot.send_message.call_count == 1
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "no" in text.lower() or "not" in text.lower()


# ── send_breaking_news_alerts ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_breaking_news_no_subscribers(tmp_db):
    """When no users opted into breaking alerts, the job exits early."""
    context = _make_context()

    with patch(
        "newsdrop.bot.jobs.load_breaking_news_subscribers", new_callable=AsyncMock
    ) as mock_load:
        mock_load.return_value = set()
        await jobs.send_breaking_news_alerts(context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_breaking_news_disabled_when_no_keywords(tmp_db):
    """If no global/custom/follow keywords, the job exits without sending."""
    context = _make_context()

    async def fake_get_prefs(chat_id, default_country="us"):
        return {
            "country": "us",
            "category": "general",
            "breaking_keywords": "",
            "breaking_use_follows": "0",
            "timezone": "UTC",
            "daily_hour": "8",
            "quiet_start_hour": "",
            "quiet_end_hour": "",
        }

    with (
        patch(
            "newsdrop.bot.jobs.load_breaking_news_subscribers", new_callable=AsyncMock
        ) as mock_load,
        patch("newsdrop.bot.jobs.BREAKING_ALERT_KEYWORDS", []),
        patch("newsdrop.bot.jobs.get_user_prefs", new_callable=AsyncMock) as mock_prefs,
        patch("newsdrop.bot.jobs.get_followed_topics", new_callable=AsyncMock, return_value=[]),
    ):
        mock_load.return_value = {100}
        mock_prefs.side_effect = fake_get_prefs
        await jobs.send_breaking_news_alerts(context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_breaking_news_sends_alerts(tmp_db):
    """Matching breaking news articles are sent to opted-in subscribers."""
    context = _make_context()

    articles = _make_articles(2, prefix="breaking")

    async def fake_get_prefs(chat_id, default_country="us"):
        return {"country": "us", "category": "general"}

    with (
        patch(
            "newsdrop.bot.jobs.load_breaking_news_subscribers", new_callable=AsyncMock
        ) as mock_load,
        patch("newsdrop.bot.jobs.get_user_prefs", new_callable=AsyncMock) as mock_prefs,
        patch("newsdrop.bot.jobs.get_followed_topics", new_callable=AsyncMock, return_value=[]),
        patch(
            "newsdrop.bot.jobs.count_breaking_alerts_today",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch("newsdrop.bot.jobs.fetch_breaking_news", new_callable=AsyncMock) as mock_fetch,
        patch(
            "newsdrop.bot.jobs.claim_breaking_alert_slot", new_callable=AsyncMock, return_value=True
        ),
        patch("newsdrop.bot.jobs.cleanup_old_breaking_alerts", new_callable=AsyncMock),
        patch("newsdrop.bot.jobs.increment", new_callable=AsyncMock),
    ):
        mock_load.return_value = {100, 101}
        mock_prefs.side_effect = fake_get_prefs
        mock_fetch.return_value = articles

        await jobs.send_breaking_news_alerts(context)

    # 2 articles × 2 subscribers × 1 compact alert each = 4
    assert context.bot.send_message.call_count == 4
    # Single-message format includes match reason + open button path
    # kwargs form: chat_id=..., text=...
    sent_texts = [(c.kwargs.get("text") or "") for c in context.bot.send_message.call_args_list]
    assert any("Breaking" in t for t in sent_texts)
    assert any("Matched" in t for t in sent_texts)


@pytest.mark.asyncio
async def test_send_breaking_news_skips_already_sent(tmp_db):
    """Articles the dedupe gate reports as already sent are not re-delivered."""
    context = _make_context()

    articles = _make_articles(2, prefix="breaking")

    async def fake_get_prefs(chat_id, default_country="us"):
        return {"country": "us", "category": "general"}

    with (
        patch(
            "newsdrop.bot.jobs.load_breaking_news_subscribers", new_callable=AsyncMock
        ) as mock_load,
        patch("newsdrop.bot.jobs.get_user_prefs", new_callable=AsyncMock) as mock_prefs,
        patch("newsdrop.bot.jobs.get_followed_topics", new_callable=AsyncMock, return_value=[]),
        patch(
            "newsdrop.bot.jobs.count_breaking_alerts_today",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch("newsdrop.bot.jobs.fetch_breaking_news", new_callable=AsyncMock) as mock_fetch,
        # Atomic claim gate: claim returns False → slot already claimed /
        # cap reached → skip sending.
        patch(
            "newsdrop.bot.jobs.claim_breaking_alert_slot", new_callable=AsyncMock
        ) as mock_mark,
        patch("newsdrop.bot.jobs.cleanup_old_breaking_alerts", new_callable=AsyncMock),
        patch("newsdrop.bot.jobs.increment", new_callable=AsyncMock),
    ):
        mock_mark.return_value = False
        mock_load.return_value = {100}
        mock_prefs.side_effect = fake_get_prefs
        mock_fetch.return_value = articles

        await jobs.send_breaking_news_alerts(context)

    # No messages sent because every article was already claimed as delivered
    context.bot.send_message.assert_not_awaited()
    # The gate was consulted once per (article, subscriber) candidate.
    assert mock_mark.await_count == len(articles)


@pytest.mark.asyncio
async def test_send_breaking_news_handles_api_error(tmp_db):
    """APIClientError during breaking news fetch is handled gracefully."""
    from newsdrop.news_fetcher import APIClientError

    context = _make_context()

    async def fake_get_prefs(chat_id, default_country="us"):
        return {"country": "us", "category": "general"}

    with (
        patch(
            "newsdrop.bot.jobs.load_breaking_news_subscribers", new_callable=AsyncMock
        ) as mock_load,
        patch("newsdrop.bot.jobs.get_user_prefs", new_callable=AsyncMock) as mock_prefs,
        patch("newsdrop.bot.jobs.get_followed_topics", new_callable=AsyncMock, return_value=[]),
        patch("newsdrop.bot.jobs.fetch_breaking_news", new_callable=AsyncMock) as mock_fetch,
        patch("newsdrop.bot.jobs.cleanup_old_breaking_alerts", new_callable=AsyncMock),
    ):
        mock_load.return_value = {100}
        mock_prefs.side_effect = fake_get_prefs
        mock_fetch.side_effect = APIClientError("timeout", status_code=504)

        # Should not raise
        await jobs.send_breaking_news_alerts(context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_breaking_news_groups_by_country(tmp_db):
    """Breaking alerts are routed only to subscribers in the article's country."""
    context = _make_context()

    us_articles = [
        {
            "title": "US Breaking",
            "description": "US news",
            "url": "https://example.com/us",
            "country": "us",
            "publishedAt": "2025-01-01T00:00:00Z",
            "source": {"name": "US Source"},
        }
    ]

    async def fake_get_prefs(chat_id, default_country="us"):
        if chat_id == 100:
            return {"country": "us", "category": "general"}
        return {"country": "gb", "category": "general"}

    with (
        patch(
            "newsdrop.bot.jobs.load_breaking_news_subscribers", new_callable=AsyncMock
        ) as mock_load,
        patch("newsdrop.bot.jobs.get_user_prefs", new_callable=AsyncMock) as mock_prefs,
        patch("newsdrop.bot.jobs.get_followed_topics", new_callable=AsyncMock, return_value=[]),
        patch(
            "newsdrop.bot.jobs.count_breaking_alerts_today",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch("newsdrop.bot.jobs.fetch_breaking_news", new_callable=AsyncMock) as mock_fetch,
        patch(
            "newsdrop.bot.jobs.claim_breaking_alert_slot", new_callable=AsyncMock, return_value=True
        ),
        patch("newsdrop.bot.jobs.cleanup_old_breaking_alerts", new_callable=AsyncMock),
        patch("newsdrop.bot.jobs.increment", new_callable=AsyncMock),
    ):
        mock_load.return_value = {100, 200}  # 100 → us, 200 → gb
        mock_prefs.side_effect = fake_get_prefs
        mock_fetch.return_value = us_articles

        await jobs.send_breaking_news_alerts(context)

    # Only chat 100 (us) should receive the us article → 1 compact alert
    assert context.bot.send_message.call_count == 1
    kwargs = context.bot.send_message.call_args.kwargs
    assert kwargs.get("chat_id") == 100
