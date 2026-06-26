"""Integration tests for bot command handlers.

These hit the real handler functions in ``bot.py`` but stub out the
``Update`` / ``ContextTypes`` objects. The bot module imports ``config``,
which requires ``NEWS_API_KEY`` to be set; the project's ``.env`` provides
that, so importing the bot module is fine.

Note on the mock shape
----------------------
The real handlers do::

    message = update.effective_message
    await message.reply_text(...)

So we wire ``update.effective_message`` to the same AsyncMock we expose as
``update.message``. That lets the test assert on ``update.message`` as the
brief requested, while still exercising the real handler code path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_update():
    """Build a fake ``Update`` whose ``effective_message`` and ``message`` are
    the same ``AsyncMock`` reply target.

    Returns the update plus the reply mock so the test can assert on it.
    """
    reply = AsyncMock()
    reply.reply_text = AsyncMock(return_value=None)

    update = MagicMock()
    update.message = reply
    # The real handlers use update.effective_message; alias it to the same mock
    # so callers can assert via either attribute.
    update.effective_message = reply
    update.effective_chat = MagicMock(id=12345)
    update.effective_user = MagicMock(id=12345, first_name="Test")

    return update, reply


def _make_context():
    """Build a minimal fake ``ContextTypes.DEFAULT_TYPE`` instance."""
    context = MagicMock()
    context.args = []
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock(return_value=None)
    return context


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


def test_help_command_responds():
    """``/help`` must call ``reply_text`` with a non-empty string that
    mentions both ``/news`` and ``/search`` (the help text does that)."""
    update, reply = _make_update()
    context = _make_context()

    asyncio.run(bot.help_command(update, context))

    reply.reply_text.assert_awaited()
    # Get the text argument from the first await call.
    sent_text = reply.reply_text.call_args.args[0]

    assert isinstance(sent_text, str) and sent_text.strip(), (
        f"expected non-empty reply text, got: {sent_text!r}"
    )
    assert "/news" in sent_text, f"expected '/news' in help text, got: {sent_text!r}"
    assert "/search" in sent_text, f"expected '/search' in help text, got: {sent_text!r}"


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


def test_start_command_responds():
    """``/start`` must call ``reply_text`` with a non-empty greeting that
    contains ``Welcome`` (per bot.py:442-450)."""
    update, reply = _make_update()
    context = _make_context()

    asyncio.run(bot.start(update, context))

    reply.reply_text.assert_awaited()
    sent_text = reply.reply_text.call_args.args[0]

    assert isinstance(sent_text, str) and sent_text.strip(), (
        f"expected non-empty greeting, got: {sent_text!r}"
    )
    assert "Welcome" in sent_text, (
        f"expected 'Welcome' in /start greeting, got: {sent_text!r}"
    )
