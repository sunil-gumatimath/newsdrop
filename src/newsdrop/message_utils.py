"""Telegram message utilities."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram import Message

MAX_MESSAGE_LENGTH = 4096

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>")
_VOID_TAGS = frozenset({"br", "hr", "img"})


def _is_inside_tag(text: str, pos: int) -> bool:
    """True when *pos* falls strictly inside an unterminated ``<...>`` tag.

    Scans the whole prefix rather than a fixed lookback window: a ``<a href>``
    tag longer than any window would otherwise be missed, producing chunks
    that begin mid-tag (``'<a '``) which Telegram rejects outright.
    """
    last_open = text.rfind("<", 0, pos)
    last_close = text.rfind(">", 0, pos)
    return last_open > last_close


def _avoid_tag_split(text: str, split_at: int) -> int:
    """If split_at lands inside an HTML tag, move it outside.

    Detects an unclosed '<' before split_at (e.g. inside '<a href="...">').
    In that case move split_at back to the opening '<' so the tag is not cut.
    """
    last_open = text.rfind("<", 0, split_at)
    last_close = text.rfind(">", 0, split_at)
    if last_open <= last_close:
        # Not inside a tag — nothing to avoid.
        return split_at
    # Inside a tag — move split before the tag if feasible.
    if last_open > 0:
        return last_open
    # Tag starts at 0 and is longer than max_length; fall through to hard split
    # after the tag close to avoid infinite loop.
    next_close = text.find(">", split_at)
    if next_close != -1 and next_close + 1 <= len(text):
        return min(next_close + 1, len(text))
    return split_at


def _open_tags(text: str) -> list[tuple[str, str]]:
    """Return the currently-open HTML tags as ``(name, raw_text)`` pairs.

    The raw text is kept so a tag re-opened on the next chunk can be
    reproduced verbatim — re-emitting a bare ``<a>`` would silently drop the
    ``href`` and turn a clickable headline into plain bold text. Innermost
    last.
    """
    stack: list[tuple[str, str]] = []
    for match in _TAG_RE.finditer(text):
        closing, name = match.group(1), match.group(2).lower()
        if closing:
            # Pop the nearest matching open tag (tolerate mismatched nesting).
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    del stack[i]
                    break
        else:
            # Treat <br> etc. as void tags that never need closing.
            if name not in _VOID_TAGS:
                stack.append((name, match.group(0)))
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
    carry_open: list[tuple[str, str]] = []  # tags left open by the previous chunk
    while text:
        # Reserve room for re-opening carried tags, so the body budget leaves
        # space for the prefix the re-balance pass prepends.
        # Bound for the *body* alone: the re-balance pass prepends ``prefix``
        # and appends closers, so both must fit inside max_length.
        prefix = "".join(raw for _name, raw in carry_open)
        prefix_len = len(prefix)
        if len(text) + prefix_len <= max_length:
            chunks.append(text)
            break
        budget = max_length - prefix_len

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

        # Never cut inside an HTML tag like <a href="...">: a partial tag makes
        # Telegram reject the whole message ("can't find end tag").
        #
        # Tag safety wins over the old "require a quarter of the budget"
        # preference — that guard could veto a correct fix and emit a chunk
        # starting mid-tag ('<a '), the exact failure being prevented here. It
        # does not win over Telegram's hard limit, though: a single tag longer
        # than the whole budget (a >4 KB href) has no tag-safe cut that fits,
        # so fall back to the budget. The cut stays >= 1, so progress is
        # guaranteed and the loop always terminates.
        if _is_inside_tag(text, split_at):
            safe = _avoid_tag_split(text, split_at)
            split_at = safe if 0 < safe <= budget else min(max(split_at, 1), budget)
        else:
            split_at = min(max(split_at, 1), budget)

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
            elif m.start() > 0:
                split_at = m.start()
            # else: the chunk is exactly one opening tag that starts at 0.
            # Keeping split_at as-is emits that tag on its own — still valid
            # HTML, and — unlike m.start() == 0 — still makes progress.

        # ``prefix`` was computed above for the budget; reuse it for the trim so the
        # open-tag set matches exactly what the re-balance pass will see.
        # Trim the body to leave room for the </tag> closers the re-balance pass
        # appends. The open set must be computed over ``prefix + body`` — the
        # same string the re-balance pass sees — because tags carried from the
        # previous chunk are still open at the end of this one even when the
        # body itself contains no markup.
        body = text[:split_at]
        combined = _open_tags(prefix + body)
        close_len = sum(len(f"</{name}>") for name, _raw in combined)
        available = max_length - len(prefix) - close_len
        if available < len(body):
            cut = max(available, 0)
            # Step back out of a tag if the trim landed inside one.
            if _is_inside_tag(body, cut):
                safe = _avoid_tag_split(body, cut)
                if 0 <= safe <= cut:
                    cut = safe
            body = body[:cut]
            combined = _open_tags(prefix + body)
        carry_open = combined

        chunks.append(body)
        text = text[len(body) :]

    # Re-balance: close tags left open at each boundary and re-open them in
    # the following chunk so every chunk is self-contained valid HTML. The raw
    # opening tag is reused verbatim, so a split <a href="..."> keeps its link.
    balanced: list[str] = []
    open_carry: list[tuple[str, str]] = []
    for chunk in chunks:
        prefix = "".join(raw for _name, raw in open_carry)
        body = prefix + chunk
        open_now = _open_tags(body)
        closing = "".join(f"</{name}>" for name, _raw in reversed(open_now))
        balanced.append(body + closing)
        open_carry = open_now
    return balanced


async def send_chunked_message(message: Message, text: str, **kwargs: Any) -> None:
    """Send a potentially long message by splitting it into chunks."""
    for chunk in chunk_message(text):
        await message.reply_text(chunk, **kwargs)
