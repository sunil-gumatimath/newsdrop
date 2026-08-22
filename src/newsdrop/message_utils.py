"""Telegram message utilities."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram import Message

MAX_MESSAGE_LENGTH = 4096

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>")
_VOID_TAGS = frozenset({"br", "hr", "img"})


def _avoid_tag_split(text: str, split_at: int) -> int:
    """If split_at lands inside an HTML tag, move it outside.

    Detects an unclosed '<' before split_at (e.g. inside '<a href="...">').
    In that case move split_at back to the opening '<' so the tag is not cut.
    """
    # Look back at most 500 chars for an opening bracket without a close.
    start = max(0, split_at - 500)
    last_open = text.rfind("<", start, split_at)
    if last_open == -1:
        return split_at
    last_close = text.rfind(">", start, split_at)
    if last_open > last_close:
        # Inside a tag — move split before the tag if feasible.
        if last_open > 0:
            return last_open
        # Tag starts at 0 and is longer than max_length; fall through to hard split
        # after the tag close to avoid infinite loop.
        next_close = text.find(">", split_at)
        if next_close != -1 and next_close + 1 <= len(text):
            return min(next_close + 1, len(text))
    return split_at


def _open_tags(text: str) -> list[str]:
    """Return the currently-open HTML tag names in *text*, innermost last."""
    stack: list[str] = []
    for match in _TAG_RE.finditer(text):
        closing, name = match.group(1), match.group(2).lower()
        if closing:
            # Pop the nearest matching open tag (tolerate mismatched nesting).
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == name:
                    del stack[i]
                    break
        else:
            # Treat <br> etc. as void tags that never need closing.
            if name not in _VOID_TAGS:
                stack.append(name)
    return stack


def chunk_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a message into chunks that fit within Telegram's character limit.

    Tries to split at paragraph boundaries (double newlines) and avoids
    cutting inside HTML tags (e.g. ``<a href="...">``). Open HTML tags are
    closed at the end of each chunk and re-opened at the start of the next
    so every chunk parses as valid HTML on its own.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    carry_open: list[str] = []  # tags left open by the previous chunk
    while text:
        budget = max_length - sum(len(f"<{t}>") + len(f"</{t}>") for t in carry_open)
        if len(text) <= budget:
            chunks.append(text)
            break

        # Try paragraph -> single newline -> space -> hard split.
        split_at = text.rfind("\n\n", 0, budget)
        if split_at != -1:
            split_at += 2  # Include the \n\n
        else:
            split_at = text.rfind("\n", 0, budget)
            if split_at != -1:
                split_at += 1  # Include the \n
            else:
                split_at = text.rfind(" ", 0, budget)
                if split_at != -1:
                    split_at += 1  # Include the space
                else:
                    split_at = budget

        # Avoid cutting inside an HTML tag like <a href="...">.
        adjusted = _avoid_tag_split(text, split_at)
        # Only use the adjusted position if it still makes solid progress;
        # require at least a quarter of the budget to avoid tiny chunks,
        # and never exceed the budget (Telegram rejects >4096).
        if adjusted != split_at and 0 < adjusted <= budget and adjusted >= budget // 4:
            split_at = adjusted
        else:
            split_at = min(split_at, budget)

        # If the chunk would end immediately after an opening tag (an empty
        # element), closing + re-opening it produces stray markup like
        # "<a ...></a>" followed by a duplicate "<a ...>". Instead, pull the
        # following non-tag text into this chunk when it fits; otherwise push
        # the whole tag to the next chunk.
        matches = list(_TAG_RE.finditer(text[:split_at]))
        m = matches[-1] if matches else None
        if (
            m is not None
            and not m.group(1)
            and m.group(2).lower() not in _VOID_TAGS
            and m.end() == split_at
        ):
            after = text[split_at:budget]
            nxt_tag = _TAG_RE.search(after)
            content = after[: nxt_tag.start()] if nxt_tag else after
            sp = content.rfind(" ")
            take = (sp + 1) if sp != -1 else len(content)
            if take > 0:
                split_at += take
            else:
                split_at = m.start()

        chunks.append(text[:split_at])
        text = text[split_at:]

    # Re-balance: close tags left open at each boundary and re-open them in
    # the following chunk so every chunk is self-contained valid HTML.
    balanced: list[str] = []
    open_carry: list[str] = []
    for chunk in chunks:
        prefix = "".join(f"<{t}>" for t in open_carry)
        body = prefix + chunk
        open_now = _open_tags(body)
        closing = "".join(f"</{t}>" for t in reversed(open_now))
        balanced.append(body + closing)
        open_carry = open_now
    return balanced


async def send_chunked_message(message: Message, text: str, **kwargs: Any) -> None:
    """Send a potentially long message by splitting it into chunks."""
    for chunk in chunk_message(text):
        await message.reply_text(chunk, **kwargs)
