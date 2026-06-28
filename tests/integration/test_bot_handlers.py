from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from newsdrop import bot


def _make_update():
    reply = AsyncMock()
    reply.reply_text = AsyncMock(return_value=None)

    update = MagicMock()
    update.message = reply
    update.effective_message = reply
    update.effective_chat = MagicMock(id=12345)
    update.effective_user = MagicMock(id=12345, first_name="Test")

    return update, reply


def _make_context():
    context = MagicMock()
    context.args = []
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock(return_value=None)
    return context


def test_help_command_responds():
    update, reply = _make_update()
    context = _make_context()

    asyncio.run(bot.help_command(update, context))

    reply.reply_text.assert_awaited()
    sent_text = reply.reply_text.call_args.args[0]

    assert isinstance(sent_text, str) and sent_text.strip(), (
        f"expected non-empty reply text, got: {sent_text!r}"
    )
    assert "/news" in sent_text, f"expected '/news' in help text, got: {sent_text!r}"
    assert "/search" in sent_text, f"expected '/search' in help text, got: {sent_text!r}"


def test_start_command_responds():
    update, reply = _make_update()
    context = _make_context()

    asyncio.run(bot.start(update, context))

    reply.reply_text.assert_awaited()
    sent_text = reply.reply_text.call_args.args[0]

    assert isinstance(sent_text, str) and sent_text.strip(), (
        f"expected non-empty greeting, got: {sent_text!r}"
    )
    assert "Welcome" in sent_text, f"expected 'Welcome' in /start greeting, got: {sent_text!r}"
