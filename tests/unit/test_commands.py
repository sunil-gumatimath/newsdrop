"""Tests for bot/commands.py — user-facing slash commands.

All external dependencies (NewsData.io API, SQLite, Redis) are mocked so the
tests run fast and deterministic without network or disk I/O (beyond the
tmp_db fixture for DB-backed commands).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from newsdrop.bot import commands

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_update(chat_id: int = 12345, user_id: int = 12345, args: list[str] | None = None):
    """Build a minimal Telegram Update mock.

    ``reply_text`` returns a mock Message that supports ``edit_text`` and
    ``delete`` so the ``/news`` and ``/health`` flows work in tests.
    """
    status_msg = AsyncMock()
    status_msg.edit_text = AsyncMock(return_value=None)
    status_msg.delete = AsyncMock(return_value=None)

    message = AsyncMock()
    message.reply_text = AsyncMock(return_value=status_msg)
    message.message_id = 999

    update = MagicMock()
    update.message = message
    update.effective_message = message
    update.effective_chat = MagicMock(id=chat_id)
    update.effective_user = MagicMock(id=user_id, first_name="Test")
    update.effective_user.id = user_id

    context = MagicMock()
    context.args = args or []
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock(return_value=None)
    context.bot.delete_message = AsyncMock(return_value=None)
    context.bot.send_photo = AsyncMock(return_value=None)

    return update, message, context


# ── /start ───────────────────────────────────────────────────────────────


def test_start_sends_welcome():
    update, message, context = _make_update()
    asyncio.run(commands.start(update, context))

    message.reply_text.assert_awaited()
    text = message.reply_text.call_args.args[0]
    assert "Welcome" in text
    assert "/news" in text
    # Guided onboarding: region keyboard attached
    kwargs = message.reply_text.call_args.kwargs
    assert kwargs.get("reply_markup") is not None


# ── /news ────────────────────────────────────────────────────────────────


def test_news_sends_digest_on_success(tmp_db):
    update, message, context = _make_update()

    fake_articles = {
        "status": "ok",
        "totalResults": 1,
        "articles": [
            {
                "title": "Test Headline",
                "description": "A test article.",
                "url": "https://example.com/1",
                "urlToImage": "",
                "publishedAt": "2025-01-01T00:00:00Z",
                "source": {"name": "TestSource"},
            }
        ],
        "sources": ["newsdata.io"],
    }

    with (
        patch("newsdrop.bot.commands.fetch_top_headlines", new_callable=AsyncMock) as mock_fetch,
        patch(
            "newsdrop.bot.commands.rate_limit_try_acquire",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "newsdrop.bot.commands.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "us", "category": "general"},
        ),
        patch("newsdrop.bot.commands.get_followed_topics", new_callable=AsyncMock, return_value=[]),
    ):
        mock_fetch.return_value = fake_articles
        asyncio.run(commands.news(update, context))

    message.reply_text.assert_awaited()
    # Verify the digest content via status_msg.edit_text
    status_msg = message.reply_text.return_value
    status_msg.edit_text.assert_awaited()
    digest_text = status_msg.edit_text.call_args.args[0]
    assert "Test Headline" in digest_text
    assert "<b>" in digest_text
    assert "TestSource" in digest_text


def test_news_chunks_long_digest(tmp_db):
    update, message, context = _make_update()

    # Build enough articles with long titles and descriptions to produce a digest > 4096 chars.
    # _format_news_digest caps at 10 articles and descriptions to 120 chars,
    # so we need long titles and source names to push past the limit.
    articles = []
    for i in range(25):
        articles.append(
            {
                "title": (
                    f"Breaking News Headline Number {i + 1}: Major Political Upheaval "
                    f"and Economic Shifts Across the Globe Today as World Leaders "
                    f"Gather for Emergency Summit Discussions"
                ),
                "description": (
                    f"Article {i + 1}: This is a very long description that provides "
                    "extensive detail about the political and economic events. It "
                    "contains comprehensive information about the events that are "
                    "shaping the world right now and could have far-reaching "
                    "consequences for international trade, diplomacy, and domestic "
                    "policy across multiple regions and countries. Experts weigh in "
                    "on the potential impacts and what this means for the future of "
                    "global cooperation and stability."
                ),
                "url": f"https://example.com/very/long/path/to/article/number/{i + 1}/detail/page",
                "urlToImage": "",
                "publishedAt": "2025-01-01T00:00:00Z",
                "source": {"name": f"InternationalNewsAgency{i + 1}GlobalReporting"},
            }
        )

    fake_articles = {
        "status": "ok",
        "totalResults": len(articles),
        "articles": articles,
        "sources": ["newsdata.io"],
    }

    with (
        patch("newsdrop.bot.commands.fetch_top_headlines", new_callable=AsyncMock) as mock_fetch,
        patch(
            "newsdrop.bot.commands.rate_limit_try_acquire",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "newsdrop.bot.commands.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "us", "category": "general"},
        ),
        patch("newsdrop.bot.commands.get_followed_topics", new_callable=AsyncMock, return_value=[]),
        patch("newsdrop.bot.commands.send_chunked_message", new_callable=AsyncMock) as mock_chunked,
    ):
        mock_fetch.return_value = fake_articles
        asyncio.run(commands.news(update, context))

    # send_chunked_message should have been called because digest > 4096 chars
    mock_chunked.assert_awaited()
    # status_msg.delete should have been called before chunked send
    status_msg = message.reply_text.return_value
    status_msg.delete.assert_awaited()


def test_news_blocks_on_cooldown(tmp_db):
    update, message, context = _make_update()

    with patch(
        "newsdrop.bot.commands.rate_limit_try_acquire",
        new_callable=AsyncMock,
        return_value=False,
    ):
        asyncio.run(commands.news(update, context))

    text = message.reply_text.call_args.args[0]
    assert "cooldown" in text.lower()


def test_news_handles_api_error(tmp_db):
    update, message, context = _make_update()

    from newsdrop.news_fetcher import APIClientError

    with (
        patch("newsdrop.bot.commands.fetch_top_headlines", new_callable=AsyncMock) as mock_fetch,
        patch(
            "newsdrop.bot.commands.rate_limit_try_acquire",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "newsdrop.bot.commands.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "us", "category": "general"},
        ),
        patch("newsdrop.bot.commands.get_followed_topics", new_callable=AsyncMock, return_value=[]),
    ):
        mock_fetch.side_effect = APIClientError("API down", status_code=500)
        asyncio.run(commands.news(update, context))

    # The error message is sent via status_msg.edit_text(), not message.reply_text()
    # status_msg is the return value of the initial reply_text call
    status_msg = message.reply_text.return_value
    edit_calls = status_msg.edit_text.call_args_list
    assert any(
        "try again" in str(c).lower() or "could not" in str(c).lower() for c in edit_calls
    ), f"Expected error message in edit_text calls, got: {edit_calls}"


# ── /search ──────────────────────────────────────────────────────────────


def test_search_requires_topic(tmp_db):
    update, message, context = _make_update(args=[])
    asyncio.run(commands.search(update, context))

    text = message.reply_text.call_args.args[0]
    assert "Usage" in text or "search" in text.lower()


def test_search_blocks_on_cooldown(tmp_db):
    update, message, context = _make_update(args=["bitcoin"])
    with patch(
        "newsdrop.bot.commands.rate_limit_try_acquire",
        new_callable=AsyncMock,
        return_value=False,
    ):
        asyncio.run(commands.search(update, context))

    text = message.reply_text.call_args.args[0]
    assert "cooldown" in text.lower() or "wait" in text.lower()


def test_search_rejects_long_query(tmp_db):
    update, message, context = _make_update(args=["x" * 201])
    asyncio.run(commands.search(update, context))

    text = message.reply_text.call_args.args[0]
    assert "too long" in text.lower()


# ── /follow ──────────────────────────────────────────────────────────────


def test_follow_adds_topic(tmp_db):
    update, message, context = _make_update(args=["AI"])
    asyncio.run(commands.follow_topic(update, context))

    text = message.reply_text.call_args.args[0]
    assert "following" in text.lower() or "✅" in text


def test_follow_rejects_empty(tmp_db):
    update, message, context = _make_update(args=[])
    asyncio.run(commands.follow_topic(update, context))

    text = message.reply_text.call_args.args[0]
    assert "Usage" in text


# ── /unfollow ────────────────────────────────────────────────────────────


def test_unfollow_removes_topic(tmp_db):
    update, message, context = _make_update(args=["AI"])
    # First follow
    asyncio.run(commands.follow_topic(update, context))
    # Then unfollow
    asyncio.run(commands.unfollow_topic(update, context))

    text = message.reply_text.call_args.args[0]
    assert "unfollowed" in text.lower() or "✅" in text


def test_unfollow_not_following(tmp_db):
    update, message, context = _make_update(args=["crypto"])
    asyncio.run(commands.unfollow_topic(update, context))

    text = message.reply_text.call_args.args[0]
    assert "not following" in text.lower()


# ── /subscribe / /unsubscribe ────────────────────────────────────────────


def test_subscribe_and_unsubscribe(tmp_db):
    update, message, context = _make_update()

    # Subscribe
    asyncio.run(commands.subscribe(update, context))
    sub_text = message.reply_text.call_args.args[0]
    assert "subscribed" in sub_text.lower() or "✅" in sub_text

    # Subscribe again — should be idempotent
    asyncio.run(commands.subscribe(update, context))
    sub_text2 = message.reply_text.call_args.args[0]
    assert "already" in sub_text2.lower()

    # Unsubscribe
    asyncio.run(commands.unsubscribe(update, context))
    unsub_text = message.reply_text.call_args.args[0]
    assert "unsubscribed" in unsub_text.lower()

    # Unsubscribe again — should tell user they're not subscribed
    asyncio.run(commands.unsubscribe(update, context))
    unsub_text2 = message.reply_text.call_args.args[0]
    assert "not subscribed" in unsub_text2.lower()


# ── /prefs ───────────────────────────────────────────────────────────────


def test_prefs_shows_user_settings(tmp_db):
    update, message, context = _make_update()

    with (
        patch(
            "newsdrop.bot.commands.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "in", "category": "technology"},
        ),
        patch(
            "newsdrop.bot.commands.get_followed_topics",
            new_callable=AsyncMock,
            return_value=["AI", "crypto"],
        ),
        patch(
            "newsdrop.bot.commands.get_breaking_news_preference",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        asyncio.run(commands.preferences(update, context))

    text = message.reply_text.call_args.args[0]
    assert "Preferences" in text
    assert "India" in text
    assert "Technology" in text


# ── /help ────────────────────────────────────────────────────────────────


def test_help_lists_commands():
    update, message, context = _make_update()
    asyncio.run(commands.help_command(update, context))

    text = message.reply_text.call_args.args[0]
    assert "/news" in text
    assert "/search" in text
    assert "/follow" in text
    assert "/help" in text


# ── /health ──────────────────────────────────────────────────────────────


def test_health_returns_status():
    update, message, context = _make_update()

    with (
        patch("newsdrop.bot.commands.is_admin_chat", return_value=True),
        patch(
            "newsdrop.bot.commands.check_api_health",
            new_callable=AsyncMock,
            return_value={"status": "healthy", "response_time": "0.5s"},
        ),
        patch(
            "newsdrop.bot.commands.check_db_health",
            new_callable=AsyncMock,
            return_value={
                "status": "healthy",
                "subscriber_count": "5",
                "followed_topic_count": "3",
                "breaking_alert_count": "10",
            },
        ),
        patch(
            "newsdrop.bot.commands.get_request_count",
            new_callable=AsyncMock,
            return_value=(42, 200),
        ),
        patch("newsdrop.bot.commands.all_metrics", new_callable=AsyncMock, return_value={}),
        patch("newsdrop.bot.commands.increment", new_callable=AsyncMock),
    ):
        asyncio.run(commands.health(update, context))

    text = message.reply_text.call_args.args[0]
    assert "Health" in text
    assert "42/200" in text


def test_health_rejects_non_admin():
    update, message, context = _make_update()

    with patch("newsdrop.bot.commands.is_admin_chat", return_value=False):
        asyncio.run(commands.health(update, context))

    text = message.reply_text.call_args.args[0]
    assert "admin" in text.lower()


# ── /trending ────────────────────────────────────────────────────────────


def test_trending_with_valid_category(tmp_db):
    update, message, context = _make_update(args=["tech"])

    with (
        patch(
            "newsdrop.bot.commands.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "us", "category": "general"},
        ),
        patch("newsdrop.bot.commands._send_trending_results", new_callable=AsyncMock) as mock_send,
    ):
        asyncio.run(commands.trending(update, context))
    mock_send.assert_awaited()


def test_trending_rejects_invalid_category(tmp_db):
    update, message, context = _make_update(args=["invalidcat"])

    asyncio.run(commands.trending(update, context))
    text = message.reply_text.call_args.args[0]
    assert "Unknown" in text or "⚠" in text


# ── /clear ───────────────────────────────────────────────────────────────


def test_clear_asks_confirmation(tmp_db):
    update, message, context = _make_update()
    asyncio.run(commands.clear_chat(update, context))

    text = message.reply_text.call_args.args[0]
    assert "clear" in text.lower() or "delete" in text.lower()
    # Should have reply_markup with confirm/cancel buttons
    markup = message.reply_text.call_args.kwargs.get("reply_markup")
    assert markup is not None


# ── /breaking ────────────────────────────────────────────────────────────


def test_breaking_shows_toggle(tmp_db):
    update, message, context = _make_update()

    with patch(
        "newsdrop.bot.commands.get_breaking_news_preference",
        new_callable=AsyncMock,
        return_value=False,
    ):
        asyncio.run(commands.breaking_toggle(update, context))

    text = message.reply_text.call_args.args[0]
    assert "Breaking" in text or "breaking" in text.lower()
