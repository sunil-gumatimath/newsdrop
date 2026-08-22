"""Regression tests for the audit fixes: chunking, ownership, whyTags, popup."""

from __future__ import annotations

import html
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from newsdrop.bot import callbacks
from newsdrop.message_utils import MAX_MESSAGE_LENGTH, chunk_message

# ---------------------------------------------------------------------------
# message_utils.chunk_message
# ---------------------------------------------------------------------------


def test_chunks_never_exceed_max_length():
    text = ("word " * 3000).strip()
    chunks = chunk_message(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_LENGTH


def test_progress_guard_not_dead():
    # A giant tag at the boundary must not cause tiny chunk cascades in the
    # NORMAL case; hard guarantees are: no chunk > 4096 and full consumption.
    tag = "<a href='" + "x" * 5000 + "'>"
    text = "start " + tag + ("tail word " * 2000)
    chunks = chunk_message(text)
    total = sum(len(c) for c in chunks)
    assert total >= len(text) - len(chunks) * 16  # only closing tags added
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_LENGTH
    # Normal text without giant tags: no tiny leading chunks.
    normal = chunk_message("para text here\n\n" * 900)
    assert all(len(c) >= MAX_MESSAGE_LENGTH // 4 for c in normal[:-1])


def test_html_tags_balanced_across_chunks():
    text = "<b>Bold intro. " + ("para text " * 900) + "</b> tail"
    chunks = chunk_message(text)
    assert len(chunks) > 1
    for i, chunk in enumerate(chunks):
        opens = chunk.count("<b>")
        closes = chunk.count("</b>")
        if i < len(chunks) - 1:
            assert opens == closes, f"chunk {i} has unbalanced tags"
        else:
            assert closes >= opens  # last chunk may close carried tags


def test_single_short_message_untouched():
    text = "<b>hello</b>"
    assert chunk_message(text) == [text]


# ---------------------------------------------------------------------------
# Callback ownership
# ---------------------------------------------------------------------------


def _make_update(data: str, from_user_id: int):
    query = MagicMock()
    query.data = data
    query.from_user.id = from_user_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 111
    return update, query


@pytest.mark.asyncio
async def test_owned_callback_rejects_foreign_tapper():
    update, query = _make_update("country:us:999", from_user_id=42)
    with patch.object(callbacks, "_handle_country_callback", new_callable=AsyncMock) as h:
        await callbacks.button_handler(update, MagicMock())
        h.assert_not_awaited()
    query.answer.assert_any_call("Not your session.", show_alert=True)


@pytest.mark.asyncio
async def test_owned_callback_accepts_owner():
    update, query = _make_update("country:us:999", from_user_id=999)
    with patch.object(callbacks, "_handle_country_callback", new_callable=AsyncMock) as h:
        await callbacks.button_handler(update, MagicMock())
        h.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_payload_still_accepted():
    update, query = _make_update("country:us", from_user_id=1)
    with patch.object(callbacks, "_handle_country_callback", new_callable=AsyncMock) as h:
        await callbacks.button_handler(update, MagicMock())
        h.assert_awaited_once()


def test_extract_ownership_user_id_variants():
    upd3, _ = _make_update("tz:Asia/Kolkata:777", 1)
    assert callbacks._extract_ownership_user_id(upd3, "tz") == 777
    upd2, _ = _make_update("tz:Asia/Kolkata", 1)
    assert callbacks._extract_ownership_user_id(upd2, "tz") is None
    updc, _ = _make_update("confirm:clear:555", 1)
    assert callbacks._extract_ownership_user_id(updc, "confirm") is None
    upds, _ = _make_update("search:bitcoin", 1)
    assert callbacks._extract_ownership_user_id(upds, "search") is None


# ---------------------------------------------------------------------------
# obnews rate limit + search popup double-answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_obnews_rate_limited_skips_fetch():
    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    with (
        patch.object(callbacks, "rate_limit_try_acquire", new=AsyncMock(return_value=False)),
        patch.object(callbacks, "fetch_top_headlines", new_callable=AsyncMock) as fetch,
    ):
        await callbacks._handle_obnews_callback(query, chat_id=1)
        fetch.assert_not_awaited()
    query.answer.assert_awaited_once()


# ---------------------------------------------------------------------------
# whyTags escaping
# ---------------------------------------------------------------------------


def test_whytags_are_html_escaped():
    article = {
        "title": "T",
        "url": "https://example.com",
        "publishedAt": "2026-08-22T00:00:00Z",
        "source": {"name": "Reuters"},
        "whyTags": ['<a href="http://evil">click</a>'],
    }
    from newsdrop.bot.helpers import _format_news_digest

    digest = _format_news_digest([article], category="general", country="us")
    escaped = html.escape('<a href="http://evil">click</a>')
    assert escaped in digest
    assert '<a href="http://evil">' not in digest
