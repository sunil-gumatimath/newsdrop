"""Tests for bot/callbacks.py — inline keyboard button handler routing.

All database functions and the news fetcher are mocked so these tests exercise
the routing logic (action dispatch, validation, confirm/cancel flow) in isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from newsdrop.bot import callbacks

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_callback_query(
    data: str,
    chat_id: int = 12345,
    user_id: int = 12345,
    message_id: int = 999,
) -> tuple[MagicMock, MagicMock]:
    """Build a Telegram Update + CallbackQuery mock.

    Returns ``(update, query)`` where ``query`` is the callback query mock.
    """
    status_msg = AsyncMock()
    status_msg.edit_text = AsyncMock(return_value=None)
    status_msg.delete = AsyncMock(return_value=None)

    message = AsyncMock()
    message.reply_text = AsyncMock(return_value=status_msg)
    message.message_id = message_id

    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = message
    query.from_user = MagicMock(id=user_id)

    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock(id=chat_id)

    return update, query


# ── button_handler routing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_button_handler_no_query():
    """If update has no callback_query, handler returns immediately."""
    update = MagicMock()
    update.callback_query = None
    context = MagicMock()

    await callbacks.button_handler(update, context)
    # No error, no interaction with context.bot
    context.bot.assert_not_called()


@pytest.mark.asyncio
async def test_button_handler_invalid_data():
    """Callback data without a colon separator is rejected."""
    update, query = _make_callback_query("invalid_no_colon")
    context = MagicMock()

    await callbacks.button_handler(update, context)

    query.edit_message_text.assert_awaited()
    text = query.edit_message_text.call_args.args[0]
    assert "invalid" in text.lower()


@pytest.mark.asyncio
async def test_button_handler_country_action(tmp_db):
    """A valid country callback sets the user's preference."""
    update, query = _make_callback_query("country:us")
    context = MagicMock()

    with (
        patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock) as mock_set,
        patch("newsdrop.bot.callbacks._country_name_from_code", return_value="United States"),
    ):
        await callbacks.button_handler(update, context)

    mock_set.assert_awaited_once()
    # set_user_prefs(chat_id, country=value) — check kwargs
    assert mock_set.call_args.kwargs.get("country") == "us" or "us" in mock_set.call_args.args
    query.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_button_handler_invalid_country_rejected(tmp_db):
    """An invalid country code is rejected without touching the DB."""
    update, query = _make_callback_query("country:zz")
    context = MagicMock()

    with patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock) as mock_set:
        await callbacks.button_handler(update, context)

    mock_set.assert_not_awaited()
    text = query.edit_message_text.call_args.args[0]
    assert "invalid" in text.lower()


@pytest.mark.asyncio
async def test_button_handler_category_action(tmp_db):
    """A valid category callback sets the user's preference."""
    update, query = _make_callback_query("category:technology")
    context = MagicMock()

    with patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock) as mock_set:
        await callbacks.button_handler(update, context)

    mock_set.assert_awaited_once()
    query.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_button_handler_invalid_category_rejected(tmp_db):
    """An invalid category is rejected without touching the DB."""
    update, query = _make_callback_query("category:invalidcat")
    context = MagicMock()

    with patch("newsdrop.bot.callbacks.set_user_prefs", new_callable=AsyncMock) as mock_set:
        await callbacks.button_handler(update, context)

    mock_set.assert_not_awaited()
    text = query.edit_message_text.call_args.args[0]
    assert "invalid" in text.lower()


@pytest.mark.asyncio
async def test_button_handler_breaking_enable(tmp_db):
    """Enabling breaking news sets the preference to True."""
    update, query = _make_callback_query("breaking:1")
    context = MagicMock()

    with patch(
        "newsdrop.bot.callbacks.set_breaking_news_preference", new_callable=AsyncMock
    ) as mock_set:
        await callbacks.button_handler(update, context)

    mock_set.assert_awaited_once()
    assert mock_set.call_args.args[1] is True


@pytest.mark.asyncio
async def test_button_handler_breaking_disable(tmp_db):
    """Disabling breaking news sets the preference to False."""
    update, query = _make_callback_query("breaking:0")
    context = MagicMock()

    with patch(
        "newsdrop.bot.callbacks.set_breaking_news_preference", new_callable=AsyncMock
    ) as mock_set:
        await callbacks.button_handler(update, context)

    mock_set.assert_awaited_once()
    assert mock_set.call_args.args[1] is False


@pytest.mark.asyncio
async def test_button_handler_follow_action(tmp_db):
    """A follow callback adds a topic for the user."""
    update, query = _make_callback_query("follow:AI")
    context = MagicMock()

    with patch("newsdrop.bot.callbacks.add_followed_topic", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = (True, "AI")
        await callbacks.button_handler(update, context)

    mock_add.assert_awaited_once()
    # reply_text is called on query.message
    query.message.reply_text.assert_awaited()


@pytest.mark.asyncio
async def test_button_handler_unfollow_action(tmp_db):
    """An unfollow callback removes a topic for the user."""
    update, query = _make_callback_query("unfollow:crypto")
    context = MagicMock()

    with patch(
        "newsdrop.bot.callbacks.remove_followed_topic", new_callable=AsyncMock
    ) as mock_remove:
        mock_remove.return_value = True
        await callbacks.button_handler(update, context)

    mock_remove.assert_awaited_once()
    query.message.reply_text.assert_awaited()


@pytest.mark.asyncio
async def test_button_handler_search_action(tmp_db):
    """A search callback triggers the news search flow."""
    update, query = _make_callback_query("search:bitcoin")
    context = MagicMock()

    with (
        patch(
            "newsdrop.bot.callbacks.rate_limit_check", new_callable=AsyncMock, return_value=False
        ),
        patch(
            "newsdrop.bot.callbacks.get_user_prefs",
            new_callable=AsyncMock,
            return_value={"country": "us", "category": "general"},
        ),
        patch("newsdrop.bot.callbacks.search_news", new_callable=AsyncMock) as mock_search,
        patch("newsdrop.bot.callbacks.format_search_results", return_value="Results for bitcoin"),
        patch("newsdrop.bot.callbacks.send_chunked_message", new_callable=AsyncMock),
        patch("newsdrop.bot.callbacks.rate_limit_record", new_callable=AsyncMock),
    ):
        mock_search.return_value = {"results": []}
        await callbacks.button_handler(update, context)

    mock_search.assert_awaited_once()


@pytest.mark.asyncio
async def test_button_handler_search_rate_limited(tmp_db):
    """A search callback blocked by cooldown does not execute the search."""
    update, query = _make_callback_query("search:bitcoin")
    context = MagicMock()

    with (
        patch("newsdrop.bot.callbacks.rate_limit_check", new_callable=AsyncMock, return_value=True),
        patch("newsdrop.bot.callbacks.search_news", new_callable=AsyncMock) as mock_search,
    ):
        await callbacks.button_handler(update, context)

    mock_search.assert_not_awaited()
    query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_button_handler_confirm_clear(tmp_db):
    """A confirm:clear action triggers chat message clearing."""
    user_id = 555
    update, query = _make_callback_query(f"confirm:clear:{user_id}:42", user_id=user_id)
    context = MagicMock()
    context.bot.delete_message = AsyncMock(return_value=None)

    with patch("newsdrop.bot.callbacks._clear_chat_messages", new_callable=AsyncMock) as mock_clear:
        mock_clear.return_value = (3, 0)
        await callbacks.button_handler(update, context)

    mock_clear.assert_awaited_once()
    query.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_button_handler_confirm_unfollow_all(tmp_db):
    """A confirm:unfollowall action clears all followed topics."""
    user_id = 555
    update, query = _make_callback_query(f"confirm:unfollowall:{user_id}", user_id=user_id)
    context = MagicMock()

    with patch(
        "newsdrop.bot.callbacks.clear_followed_topics", new_callable=AsyncMock
    ) as mock_clear:
        mock_clear.return_value = 5
        await callbacks.button_handler(update, context)

    mock_clear.assert_awaited_once()
    query.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_button_handler_confirm_wrong_user(tmp_db):
    """A confirm action from a different user is rejected."""
    update, query = _make_callback_query(
        "confirm:clear:999:42",
        user_id=111,  # user_id mismatch
    )
    context = MagicMock()

    with patch("newsdrop.bot.callbacks._clear_chat_messages", new_callable=AsyncMock) as mock_clear:
        await callbacks.button_handler(update, context)

    mock_clear.assert_not_awaited()
    query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_button_handler_cancel(tmp_db):
    """A cancel action aborts the flow."""
    user_id = 555
    update, query = _make_callback_query(f"cancel:something:{user_id}", user_id=user_id)
    context = MagicMock()

    with patch(
        "newsdrop.bot.callbacks.clear_followed_topics", new_callable=AsyncMock
    ) as mock_clear:
        await callbacks.button_handler(update, context)

    mock_clear.assert_not_awaited()
    query.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_button_handler_unknown_action(tmp_db):
    """An unrecognized action is rejected."""
    update, query = _make_callback_query("unknown:value")
    context = MagicMock()

    await callbacks.button_handler(update, context)

    query.edit_message_text.assert_awaited()
    text = query.edit_message_text.call_args.args[0]
    assert "unsupported" in text.lower() or "unhandled" in text.lower()
