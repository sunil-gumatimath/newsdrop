"""Handler-level tests closing the coverage gap on commands, callbacks, jobs,
main.error_handler, and the health-server auth/metrics endpoints.

Follows the conventions of test_commands.py / test_callbacks.py: Telegram
objects are MagicMock/AsyncMock, the DB uses the real tmp_db fixture, and
network-facing fetchers are patched.
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from newsdrop.bot import callbacks, commands, helpers, jobs
from newsdrop.bot.helpers import DigestResult
from newsdrop.news_fetcher import APIClientError

# bot/__init__.py re-exports the main() *function* under the name ``main``,
# shadowing the submodule — import the module object explicitly for tests.
bot_main = importlib.import_module("newsdrop.bot.main")

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_update(chat_id: int = 12345, user_id: int = 12345, args: list[str] | None = None):
    status_msg = AsyncMock()
    status_msg.edit_text = AsyncMock(return_value=None)
    status_msg.delete = AsyncMock(return_value=None)

    message = AsyncMock()
    message.reply_text = AsyncMock(return_value=status_msg)
    message.reply_document = AsyncMock(return_value=None)
    message.message_id = 999

    update = MagicMock()
    update.message = message
    update.effective_message = message
    update.effective_chat = MagicMock(id=chat_id)
    update.effective_user = MagicMock(id=user_id, first_name="Test")

    context = MagicMock()
    context.args = args or []
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock(return_value=None)
    context.bot.delete_message = AsyncMock(return_value=None)

    return update, message, context


def _make_callback_query(
    data: str,
    chat_id: int = 12345,
    user_id: int = 12345,
    message_id: int = 999,
):
    status_msg = AsyncMock()
    status_msg.edit_text = AsyncMock(return_value=None)

    message = AsyncMock()
    message.reply_text = AsyncMock(return_value=status_msg)
    message.message_id = message_id

    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.delete_message = AsyncMock()
    query.message = message
    query.from_user = MagicMock(id=user_id)

    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock(id=chat_id)
    return update, query


# ── /setcountry · /setcategory · /settime · /settimezone ────────────────


async def test_set_country_shows_keyboard(tmp_db):
    update, message, context = _make_update()
    await commands.set_country(update, context)
    text = message.reply_text.call_args.args[0]
    assert "region" in text.lower()
    assert message.reply_text.call_args.kwargs.get("reply_markup") is not None


async def test_set_category_shows_keyboard(tmp_db):
    update, message, context = _make_update()
    await commands.set_category(update, context)
    text = message.reply_text.call_args.args[0]
    assert "category" in text.lower()
    assert message.reply_text.call_args.kwargs.get("reply_markup") is not None


async def test_set_time_shows_hours(tmp_db):
    update, message, context = _make_update()
    await commands.set_time(update, context)
    text = message.reply_text.call_args.args[0]
    assert "digest hour" in text.lower()
    assert message.reply_text.call_args.kwargs.get("reply_markup") is not None


async def test_set_timezone_valid_arg(tmp_db):
    update, message, context = _make_update(args=["Europe/Paris"])
    await commands.set_timezone(update, context)
    text = message.reply_text.call_args.args[0]
    assert "Paris" in text


async def test_set_timezone_invalid_arg(tmp_db):
    update, message, context = _make_update(args=["Mars/Olympus"])
    await commands.set_timezone(update, context)
    # The handler warns AND falls through to the picker keyboard.
    all_texts = [c.args[0] for c in message.reply_text.call_args_list]
    assert any("unknown timezone" in t.lower() for t in all_texts)
    assert message.reply_text.call_args.kwargs.get("reply_markup") is not None


async def test_set_timezone_no_args_shows_keyboard(tmp_db):
    update, message, context = _make_update()
    await commands.set_timezone(update, context)
    assert message.reply_text.call_args.kwargs.get("reply_markup") is not None


# ── /setfreq ─────────────────────────────────────────────────────────────


async def test_setfreq_arg_daily(tmp_db):
    update, message, context = _make_update(args=["daily"])
    await commands.set_freq(update, context)
    text = message.reply_text.call_args.args[0]
    assert "daily" in text.lower()


async def test_setfreq_alias_weekday(tmp_db):
    update, message, context = _make_update(args=["weekday"])
    await commands.set_freq(update, context)
    text = message.reply_text.call_args.args[0]
    assert "weekdays" in text.lower()


async def test_setfreq_custom_with_days(tmp_db):
    update, message, context = _make_update(args=["custom", "0,2,4"])
    await commands.set_freq(update, context)
    text = message.reply_text.call_args.args[0]
    assert "custom" in text.lower()


async def test_setfreq_custom_no_days_shows_picker(tmp_db):
    update, message, context = _make_update(args=["custom"])
    await commands.set_freq(update, context)
    markup = message.reply_text.call_args.kwargs.get("reply_markup")
    assert markup is not None


async def test_setfreq_unknown_arg(tmp_db):
    update, message, context = _make_update(args=["hourly"])
    await commands.set_freq(update, context)
    text = message.reply_text.call_args.args[0]
    assert "unknown frequency" in text.lower()


async def test_setfreq_no_args_shows_keyboard(tmp_db):
    update, message, context = _make_update()
    await commands.set_freq(update, context)
    assert message.reply_text.call_args.kwargs.get("reply_markup") is not None


# ── /quiet ───────────────────────────────────────────────────────────────


async def test_quiet_hours_status(tmp_db):
    update, message, context = _make_update()
    await commands.quiet_hours(update, context)
    text = message.reply_text.call_args.args[0]
    assert "quiet hours" in text.lower()


async def test_quiet_hours_off(tmp_db):
    update, message, context = _make_update(args=["off"])
    await commands.quiet_hours(update, context)
    assert "disabled" in message.reply_text.call_args.args[0].lower()


async def test_quiet_hours_set_valid(tmp_db):
    update, message, context = _make_update(args=["22", "7"])
    await commands.quiet_hours(update, context)
    text = message.reply_text.call_args.args[0]
    assert "22:00–7:00" in text


async def test_quiet_hours_equal_rejected(tmp_db):
    update, message, context = _make_update(args=["7", "7"])
    await commands.quiet_hours(update, context)
    assert "differ" in message.reply_text.call_args.args[0].lower()


async def test_quiet_hours_out_of_range(tmp_db):
    update, message, context = _make_update(args=["25", "7"])
    await commands.quiet_hours(update, context)
    assert "between 0 and 23" in message.reply_text.call_args.args[0]


async def test_quiet_hours_non_integer(tmp_db):
    update, message, context = _make_update(args=["abc", "7"])
    await commands.quiet_hours(update, context)
    assert "integers" in message.reply_text.call_args.args[0].lower()


async def test_quiet_hours_missing_end(tmp_db):
    update, message, context = _make_update(args=["22"])
    await commands.quiet_hours(update, context)
    assert "start and end" in message.reply_text.call_args.args[0].lower()


# ── /breakkeywords ───────────────────────────────────────────────────────


async def test_breakkeywords_status_empty(tmp_db):
    update, message, context = _make_update()
    await commands.breakkeywords(update, context)
    text = message.reply_text.call_args.args[0]
    assert "breaking keywords" in text.lower()


async def test_breakkeywords_add_and_duplicate(tmp_db):
    update, message, context = _make_update(args=["add", "climate"])
    await commands.breakkeywords(update, context)
    assert "added" in message.reply_text.call_args.args[0].lower()

    update2, message2, context2 = _make_update(args=["add", "climate"])
    await commands.breakkeywords(update2, context2)
    assert "already" in message2.reply_text.call_args.args[0].lower()


async def test_breakkeywords_add_too_short(tmp_db):
    update, message, context = _make_update(args=["add", "ab"])
    await commands.breakkeywords(update, context)
    assert "3 characters" in message.reply_text.call_args.args[0]


async def test_breakkeywords_add_without_keyword(tmp_db):
    update, message, context = _make_update(args=["add"])
    await commands.breakkeywords(update, context)
    assert "provide a keyword" in message.reply_text.call_args.args[0].lower()


async def test_breakkeywords_remove(tmp_db):
    update, message, context = _make_update(args=["add", "flood"])
    await commands.breakkeywords(update, context)
    update2, message2, context2 = _make_update(args=["remove", "flood"])
    await commands.breakkeywords(update2, context2)
    assert "removed" in message2.reply_text.call_args.args[0].lower()


async def test_breakkeywords_remove_missing(tmp_db):
    update, message, context = _make_update(args=["remove", "ghost"])
    await commands.breakkeywords(update, context)
    assert "not in your list" in message.reply_text.call_args.args[0].lower()


async def test_breakkeywords_clear(tmp_db):
    update, message, context = _make_update(args=["clear"])
    await commands.breakkeywords(update, context)
    assert "cleared" in message.reply_text.call_args.args[0].lower()


async def test_breakkeywords_unknown_action(tmp_db):
    update, message, context = _make_update(args=["fetch", "x"])
    await commands.breakkeywords(update, context)
    assert "unknown action" in message.reply_text.call_args.args[0].lower()


# ── follows listing + unfollowall prompt ─────────────────────────────────


async def test_list_followed_topics_empty_and_filled(tmp_db):
    update, message, context = _make_update()
    await commands.list_followed_topics(update, context)
    assert "not following" in message.reply_text.call_args.args[0].lower()

    update2, message2, context2 = _make_update(args=["AI"])
    await commands.follow_topic(update2, context2)
    update3, message3, context3 = _make_update()
    await commands.list_followed_topics(update3, context3)
    assert "AI" in message3.reply_text.call_args.args[0]


async def test_unfollow_all_asks_confirmation(tmp_db):
    update, message, context = _make_update()
    await commands.unfollow_all_topics(update, context)
    markup = message.reply_text.call_args.kwargs.get("reply_markup")
    assert markup is not None


# ── /search success + error + empty ──────────────────────────────────────


async def test_search_success(tmp_db):
    update, message, context = _make_update(args=["bitcoin"])
    payload = DigestResult("<b>Search results</b>", None, None)
    with (
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
        patch(
            "newsdrop.bot.commands.search_news",
            new_callable=AsyncMock,
            return_value={"articles": [], "totalResults": 0},
        ),
        patch("newsdrop.bot.commands.build_search_payload", return_value=payload),
    ):
        await commands.search(update, context)
    status_msg = message.reply_text.return_value
    assert "Search results" in status_msg.edit_text.call_args.args[0]


async def test_search_api_error(tmp_db):
    update, message, context = _make_update(args=["bitcoin"])
    with (
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
        patch(
            "newsdrop.bot.commands.search_news",
            new_callable=AsyncMock,
            side_effect=APIClientError("boom", status_code=500),
        ),
    ):
        await commands.search(update, context)
    status_msg = message.reply_text.return_value
    assert "could not fetch" in status_msg.edit_text.call_args.args[0].lower()


async def test_search_empty_results_message(tmp_db):
    update, message, context = _make_update(args=["xyzzy"])
    payload = DigestResult(None, "<b>No results</b>", None)
    with (
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
        patch(
            "newsdrop.bot.commands.search_news",
            new_callable=AsyncMock,
            return_value={"articles": [], "totalResults": 0},
        ),
        patch("newsdrop.bot.commands.build_search_payload", return_value=payload),
    ):
        await commands.search(update, context)
    status_msg = message.reply_text.return_value
    assert "No results" in status_msg.edit_text.call_args.args[0]


# ── /news empty state ────────────────────────────────────────────────────


async def test_news_empty_state(tmp_db):
    update, message, context = _make_update()
    payload = DigestResult(None, "📭 <b>No stories found</b>", None)
    with (
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
        patch(
            "newsdrop.bot.commands.fetch_top_headlines",
            new_callable=AsyncMock,
            return_value={"articles": [], "sources": []},
        ),
        patch("newsdrop.bot.commands._build_digest_payload", return_value=payload),
    ):
        await commands.news(update, context)
    status_msg = message.reply_text.return_value
    assert "No stories found" in status_msg.edit_text.call_args.args[0]


# ── /trending ────────────────────────────────────────────────────────────


async def test_trending_country_override(tmp_db):
    update, message, context = _make_update(args=["tech", "in"])
    with (
        patch(
            "newsdrop.bot.commands.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "us", "category": "general"},
        ),
        patch("newsdrop.bot.commands._send_trending_results", new_callable=AsyncMock) as mock_send,
    ):
        await commands.trending(update, context)
    assert mock_send.call_args.args[3] == "in"


async def test_trending_rate_limited(tmp_db):
    update, message, context = _make_update(args=["tech"])
    with patch(
        "newsdrop.bot.commands.rate_limit_try_acquire",
        new_callable=AsyncMock,
        return_value=False,
    ):
        await commands.trending(update, context)
    assert "cooldown" in message.reply_text.call_args.args[0].lower()


async def test_send_trending_results_success(tmp_db):
    update, message, context = _make_update()
    with (
        patch(
            "newsdrop.bot.helpers.fetch_trending_topics",
            new_callable=AsyncMock,
            return_value={"bitcoin": 5, "climate": 3},
        ),
        patch(
            "newsdrop.bot.helpers.is_following_topic", new_callable=AsyncMock, return_value=False
        ),
    ):
        await helpers._send_trending_results(message, 12345, "general", "us")
    # Status placeholder deleted, results sent with a keyboard.
    message.reply_text.assert_awaited()
    final_kwargs = message.reply_text.call_args.kwargs
    assert final_kwargs.get("reply_markup") is not None


async def test_send_trending_results_error(tmp_db):
    update, message, context = _make_update()
    status_msg = message.reply_text.return_value
    with patch(
        "newsdrop.bot.helpers.fetch_trending_topics",
        new_callable=AsyncMock,
        side_effect=RuntimeError("down"),
    ):
        await helpers._send_trending_results(message, 12345, "general", "us")
    status_msg.edit_text.assert_awaited()
    assert "failed" in status_msg.edit_text.call_args.args[0].lower()


# ── /export ──────────────────────────────────────────────────────────────


async def test_export_success(tmp_db):
    update, message, context = _make_update()
    with (
        patch(
            "newsdrop.bot.commands.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "us", "category": "general"},
        ),
        patch("newsdrop.bot.commands.get_followed_topics", new_callable=AsyncMock, return_value=[]),
        patch(
            "newsdrop.bot.commands.fetch_top_headlines",
            new_callable=AsyncMock,
            return_value={"articles": [], "sources": []},
        ),
    ):
        await commands.export_briefing(update, context)
    message.reply_document.assert_awaited_once()


async def test_export_api_error(tmp_db):
    update, message, context = _make_update()
    with (
        patch(
            "newsdrop.bot.commands.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "us", "category": "general"},
        ),
        patch("newsdrop.bot.commands.get_followed_topics", new_callable=AsyncMock, return_value=[]),
        patch(
            "newsdrop.bot.commands.fetch_top_headlines",
            new_callable=AsyncMock,
            side_effect=APIClientError("boom", status_code=429),
        ),
    ):
        await commands.export_briefing(update, context)
    status_msg = message.reply_text.return_value
    assert "could not fetch" in status_msg.edit_text.call_args.args[0].lower()


async def test_export_unexpected_error(tmp_db):
    update, message, context = _make_update()
    with (
        patch(
            "newsdrop.bot.commands.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "us", "category": "general"},
        ),
        patch("newsdrop.bot.commands.get_followed_topics", new_callable=AsyncMock, return_value=[]),
        patch(
            "newsdrop.bot.commands.fetch_top_headlines",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kaboom"),
        ),
    ):
        await commands.export_briefing(update, context)
    status_msg = message.reply_text.return_value
    assert "unexpected" in status_msg.edit_text.call_args.args[0].lower()


# ── /health unhealthy-DB branch ──────────────────────────────────────────


async def test_health_db_unhealthy_branch():
    update, message, context = _make_update()
    with (
        patch("newsdrop.bot.commands.is_admin_chat", return_value=True),
        patch(
            "newsdrop.bot.commands.check_api_health",
            new_callable=AsyncMock,
            return_value={"status": "unhealthy", "error": "timeout"},
        ),
        patch(
            "newsdrop.bot.commands.check_db_health",
            new_callable=AsyncMock,
            return_value={"status": "unhealthy", "error": "disk"},
        ),
        patch(
            "newsdrop.bot.commands.get_request_count",
            new_callable=AsyncMock,
            return_value=(0, 200),
        ),
        patch("newsdrop.bot.commands.all_metrics", new_callable=AsyncMock, return_value={}),
        patch("newsdrop.bot.commands.increment", new_callable=AsyncMock),
    ):
        await commands.health(update, context)
    text = message.reply_text.call_args.args[0]
    assert "0/200" in text


# ── callbacks: onboarding, menus, prefs, ownership ───────────────────────


async def test_callback_obcountry_valid(tmp_db):
    update, query = _make_callback_query("obcountry:in:12345")
    with patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock):
        await callbacks.button_handler(update, MagicMock())
    text = query.edit_message_text.call_args.args[0]
    assert "Step 2" in text


async def test_callback_obcategory_valid(tmp_db):
    update, query = _make_callback_query("obcategory:science:12345")
    with (
        patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock),
        patch(
            "newsdrop.bot.callbacks.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "in", "category": "science"},
        ),
    ):
        await callbacks.button_handler(update, MagicMock())
    text = query.edit_message_text.call_args.args[0]
    assert "Step 3" in text


async def test_callback_obsub_subscribe(tmp_db):
    update, query = _make_callback_query("obsub:1:12345")
    with patch(
        "newsdrop.bot.callbacks.get_user_prefs",
        new_callable=AsyncMock,
        return_value={"country": "in", "category": "science", "daily_hour": "8", "timezone": "UTC"},
    ):
        await callbacks.button_handler(update, MagicMock())
    assert "You're set" in query.edit_message_text.call_args.args[0]


async def test_callback_obsub_skip_mentions_settime(tmp_db):
    update, query = _make_callback_query("obsub:0:12345")
    await callbacks.button_handler(update, MagicMock())
    text = query.edit_message_text.call_args.args[0]
    assert "/settime" in text
    assert "/subscribe" not in text


async def test_callback_obnews_success(tmp_db):
    update, query = _make_callback_query("obnews:1")
    payload = DigestResult("<b>Digest</b>", None, None)
    with (
        patch(
            "newsdrop.bot.callbacks.rate_limit_try_acquire",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "newsdrop.bot.callbacks.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "us", "category": "general"},
        ),
        patch(
            "newsdrop.bot.callbacks.get_followed_topics", new_callable=AsyncMock, return_value=[]
        ),
        patch(
            "newsdrop.bot.callbacks.fetch_top_headlines",
            new_callable=AsyncMock,
            return_value={"articles": [], "sources": []},
        ),
        patch("newsdrop.bot.callbacks._build_digest_payload", return_value=payload),
    ):
        await callbacks.button_handler(update, MagicMock())
    assert "Digest" in query.edit_message_text.call_args.args[0]


async def test_callback_obnews_rate_limited(tmp_db):
    update, query = _make_callback_query("obnews:1")
    with patch(
        "newsdrop.bot.callbacks.rate_limit_try_acquire",
        new_callable=AsyncMock,
        return_value=False,
    ):
        await callbacks.button_handler(update, MagicMock())
    query.answer.assert_awaited()


async def test_callback_menu_actions(tmp_db):
    for value, expected in (
        ("country", "region"),
        ("category", "category"),
        ("search_hint", "/search"),
    ):
        update, query = _make_callback_query(f"menu:{value}")
        await callbacks.button_handler(update, MagicMock())
        assert expected.lower() in query.edit_message_text.call_args.args[0].lower()


async def test_callback_breakfollows_toggle(tmp_db):
    update, query = _make_callback_query("breakfollows:1")
    with patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock) as mock_set:
        await callbacks.button_handler(update, MagicMock())
    assert mock_set.call_args.kwargs.get("breaking_use_follows") is True


async def test_callback_dailyhour_valid_and_invalid(tmp_db):
    update, query = _make_callback_query("dailyhour:21")
    with patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock) as mock_set:
        await callbacks.button_handler(update, MagicMock())
    assert mock_set.call_args.kwargs.get("daily_hour") == 21

    update2, query2 = _make_callback_query("dailyhour:99")
    await callbacks.button_handler(update2, MagicMock())
    assert "invalid" in query2.edit_message_text.call_args.args[0].lower()


async def test_callback_tz_valid_and_invalid(tmp_db):
    update, query = _make_callback_query("tz:Asia/Tokyo")
    with patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock) as mock_set:
        await callbacks.button_handler(update, MagicMock())
    assert mock_set.call_args.kwargs.get("timezone") == "Asia/Tokyo"

    update2, query2 = _make_callback_query("tz:Not/AZone")
    await callbacks.button_handler(update2, MagicMock())
    assert "invalid" in query2.edit_message_text.call_args.args[0].lower()


async def test_callback_freq_non_custom(tmp_db):
    update, query = _make_callback_query("freq:twice")
    with patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock) as mock_set:
        await callbacks.button_handler(update, MagicMock())
    assert mock_set.call_args.kwargs.get("digest_frequency") == "twice"


async def test_callback_freq_custom_shows_day_picker(tmp_db):
    update, query = _make_callback_query("freq:custom")
    with patch(
        "newsdrop.bot.callbacks.get_user_prefs",
        new_callable=AsyncMock,
        return_value={"digest_days": ""},
    ):
        await callbacks.button_handler(update, MagicMock())
    assert query.edit_message_text.call_args.kwargs.get("reply_markup") is not None


async def test_callback_freq_invalid(tmp_db):
    update, query = _make_callback_query("freq:hourly")
    await callbacks.button_handler(update, MagicMock())
    assert "invalid" in query.edit_message_text.call_args.args[0].lower()


async def test_callback_freqday_toggle(tmp_db):
    update, query = _make_callback_query("freqday:3")
    with (
        patch(
            "newsdrop.bot.callbacks.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"digest_days": ""},
        ),
        patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock) as mock_set,
    ):
        await callbacks.button_handler(update, MagicMock())
    assert mock_set.call_args.kwargs.get("digest_days") == "3"

    update2, query2 = _make_callback_query("freqday:9")
    await callbacks.button_handler(update2, MagicMock())
    assert "invalid" in query2.edit_message_text.call_args.args[0].lower()


async def test_callback_freqdays_done(tmp_db):
    update, query = _make_callback_query("freqdays:done")
    # The handler imports get_user_prefs from ..database at call time.
    with patch(
        "newsdrop.database.get_user_prefs",
        new_callable=AsyncMock,
        return_value={"digest_days": "1,3"},
    ):
        await callbacks.button_handler(update, MagicMock())
    assert "Tue" in query.edit_message_text.call_args.args[0]


async def test_callback_freqdays_done_empty(tmp_db):
    update, query = _make_callback_query("freqdays:done")
    with patch(
        "newsdrop.database.get_user_prefs",
        new_callable=AsyncMock,
        return_value={"digest_days": ""},
    ):
        await callbacks.button_handler(update, MagicMock())
    assert "at least one day" in query.edit_message_text.call_args.args[0].lower()


async def test_callback_ownership_rejects_other_user(tmp_db):
    update, query = _make_callback_query("country:us:111", user_id=222)
    with patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock) as mock_set:
        await callbacks.button_handler(update, MagicMock())
    mock_set.assert_not_awaited()
    query.answer.assert_awaited()


async def test_callback_ownership_suffix_stripped_for_validation(tmp_db):
    """`country:in:<uid>` tapped by the owner must validate as `in`."""
    update, query = _make_callback_query("country:in:12345", user_id=12345)
    with patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock) as mock_set:
        await callbacks.button_handler(update, MagicMock())
    mock_set.assert_awaited_once()
    assert mock_set.call_args.kwargs.get("country") == "in"


# ── jobs: digest scheduling + alert keyword logic ────────────────────────


def _prefs(**overrides):
    base = {
        "timezone": "UTC",
        "daily_hour": "8",
        "digest_frequency": "daily",
        "digest_days": "",
        "quiet_start_hour": "",
        "quiet_end_hour": "",
        "breaking_keywords": "",
        "breaking_use_follows": "1",
    }
    base.update(overrides)
    return base


def test_is_digest_due_daily():
    from datetime import UTC, datetime

    now = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
    assert jobs.is_digest_due(_prefs(daily_hour="8"), now) is True
    assert jobs.is_digest_due(_prefs(daily_hour="9"), now) is False


def test_is_digest_due_twice_uses_fixed_hours():
    from datetime import UTC, datetime

    at_8 = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
    at_9 = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    prefs = _prefs(digest_frequency="twice", daily_hour="6")
    assert jobs.is_digest_due(prefs, at_8) is True
    assert jobs.is_digest_due(prefs, at_9) is False


def test_is_digest_due_weekdays_skips_weekend():
    from datetime import UTC, datetime

    saturday = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)  # a Saturday
    monday = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    prefs = _prefs(digest_frequency="weekdays")
    assert jobs.is_digest_due(prefs, saturday) is False
    assert jobs.is_digest_due(prefs, monday) is True


def test_is_digest_due_custom_days():
    from datetime import UTC, datetime

    monday = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    tuesday = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
    prefs = _prefs(digest_frequency="custom", digest_days="0")
    assert jobs.is_digest_due(prefs, monday) is True
    assert jobs.is_digest_due(prefs, tuesday) is False


def test_is_digest_due_custom_empty_days_falls_back_daily():
    from datetime import UTC, datetime

    monday = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    assert jobs.is_digest_due(_prefs(digest_frequency="custom", digest_days=""), monday) is True


def test_is_digest_due_invalid_values_fall_back():
    from datetime import UTC, datetime

    now = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
    assert jobs.is_digest_due(_prefs(digest_frequency="nonsense"), now) is True
    assert jobs.is_digest_due(_prefs(daily_hour="notanumber"), now) is True


def test_is_in_quiet_hours():
    from datetime import UTC, datetime

    at_noon = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    at_23 = datetime(2026, 10, 6, 23, 0, tzinfo=UTC)
    at_6 = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
    inside = _prefs(quiet_start_hour="22", quiet_end_hour="7")
    assert jobs.is_in_quiet_hours(inside, at_23) is True
    assert jobs.is_in_quiet_hours(inside, at_6) is True
    assert jobs.is_in_quiet_hours(inside, at_noon) is False
    assert jobs.is_in_quiet_hours(_prefs(), at_23) is False
    assert jobs.is_in_quiet_hours(_prefs(quiet_start_hour="x", quiet_end_hour="7"), at_23) is False


def test_resolve_alert_keywords_priority():
    # Custom keywords win; follows added; defaults only when nothing personal.
    prefs = _prefs(breaking_keywords="quake, flood", breaking_use_follows="1")
    assert jobs.resolve_alert_keywords(prefs, ["quake", "ai"]) == ["quake", "flood", "ai"]

    prefs_no_follows = _prefs(breaking_keywords="quake", breaking_use_follows="0")
    assert jobs.resolve_alert_keywords(prefs_no_follows, ["ai"]) == ["quake"]

    empty = _prefs()
    resolved = jobs.resolve_alert_keywords(empty, [])
    assert resolved  # falls back to global defaults


def test_matching_alert_keywords_rules():
    title_hit = {"title": "Earthquake hits coast", "description": "mild weather"}
    assert jobs.matching_alert_keywords(title_hit, ["earthquake"]) == ["earthquake"]

    single_body_hit = {"title": "Quiet day", "description": "talked about war history"}
    assert jobs.matching_alert_keywords(single_body_hit, ["war"]) == []

    double_body_hit = {"title": "Quiet day", "description": "war and flood coverage"}
    assert jobs.matching_alert_keywords(double_body_hit, ["war", "flood"]) == ["war", "flood"]

    assert jobs.matching_alert_keywords(title_hit, []) == []


# ── main.error_handler ───────────────────────────────────────────────────


async def test_error_handler_replies_to_user():
    from telegram import Update

    status_msg = AsyncMock()
    message = AsyncMock()
    message.reply_text = AsyncMock(return_value=status_msg)
    # error_handler isinstance-checks the update, so use a real Update object
    # (PTB v22 Update uses __slots__ — pass the message via the constructor).
    update = Update(update_id=1, message=message)

    context = MagicMock()
    context.error = RuntimeError("boom")

    with patch.object(bot_main, "increment", new_callable=AsyncMock):
        await bot_main.error_handler(update, context)

    message.reply_text.assert_awaited_once()


async def test_error_handler_ignores_non_update():
    context = MagicMock()
    context.error = RuntimeError("boom")
    with patch.object(bot_main, "increment", new_callable=AsyncMock):
        # Plain objects (not Update) must not raise.
        await bot_main.error_handler(object(), context)


# ── health server: metrics + auth ────────────────────────────────────────


class TestHealthServerExtras:
    def test_metrics_endpoint_returns_counters(self):
        from newsdrop.bot import health_server

        server = health_server.start_health_server(port=18085)
        try:
            import urllib.request

            resp = urllib.request.urlopen("http://localhost:18085/metrics", timeout=3)
            body = json.loads(resp.read())
            assert resp.status == 200
            assert body["status"] == "ok"
            assert "metrics" in body and "rates" in body
        finally:
            server.shutdown()

    def test_metrics_endpoint_requires_token_when_set(self, monkeypatch):
        import urllib.error
        import urllib.request

        from newsdrop.bot import health_server

        monkeypatch.setattr(health_server, "HEALTH_TOKEN", "s3cret")
        server = health_server.start_health_server(port=18086)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen("http://localhost:18086/health", timeout=3)
            assert exc_info.value.code == 401

            req = urllib.request.Request(
                "http://localhost:18086/health",
                headers={"Authorization": "Bearer s3cret"},
            )
            resp = urllib.request.urlopen(req, timeout=3)
            assert resp.status == 200
        finally:
            server.shutdown()
            monkeypatch.setattr(health_server, "HEALTH_TOKEN", "")
