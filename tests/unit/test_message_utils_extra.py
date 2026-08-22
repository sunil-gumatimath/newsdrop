"""Additional unit tests for message_utils.py chunking edge cases."""

from __future__ import annotations

from unittest.mock import AsyncMock

from newsdrop.message_utils import _avoid_tag_split, chunk_message, send_chunked_message

# ── _avoid_tag_split ────────────────────────────────────────────────────


def test_avoid_tag_split_no_open_bracket():
    text = "plain text only"
    assert _avoid_tag_split(text, 8) == 8


def test_avoid_tag_split_moves_before_unclosed_tag():
    text = 'hello <a href="https://example.com">link</a> tail'
    # Split lands inside the href attribute.
    split_at = text.index("example")
    adjusted = _avoid_tag_split(text, split_at)
    assert adjusted == text.index("<a ")


def test_avoid_tag_split_closed_tag_untouched():
    text = "<b>bold</b> and more text here"
    split_at = text.index("and")
    assert _avoid_tag_split(text, split_at) == split_at


def test_avoid_tag_split_tag_at_start_falls_forward():
    text = '<a href="https://example.com/very/long/path">x'
    # Split inside the tag that starts at position 0.
    split_at = 10
    adjusted = _avoid_tag_split(text, split_at)
    # Falls forward to just after the tag's closing bracket.
    assert adjusted == text.index(">") + 1


# ── chunk_message ───────────────────────────────────────────────────────


def test_chunk_message_prefers_paragraph_breaks():
    part1 = "A" * 30
    part2 = "B" * 30
    part3 = "C" * 30
    text = f"{part1}\n\n{part2}\n\n{part3}"
    chunks = chunk_message(text, max_length=50)
    assert len(chunks) == 3
    # Each chunk breaks at a paragraph boundary, keeping the separator.
    assert chunks[0] == part1 + "\n\n"
    assert chunks[1] == part2 + "\n\n"
    assert chunks[2] == part3


def test_chunk_message_falls_back_to_single_newline():
    line1 = "A" * 30
    line2 = "B" * 30
    line3 = "C" * 30
    text = f"{line1}\n{line2}\n{line3}"
    chunks = chunk_message(text, max_length=65)
    assert len(chunks) >= 2
    assert chunks[0].endswith("\n")


def test_chunk_message_falls_back_to_space():
    word_a = "A" * 20
    word_b = "B" * 20
    word_c = "C" * 20
    text = f"{word_a} {word_b} {word_c}"
    chunks = chunk_message(text, max_length=45)
    assert len(chunks) >= 2
    assert chunks[0].endswith(" ")


def test_chunk_message_hard_split_without_separators():
    text = "X" * 100
    chunks = chunk_message(text, max_length=30)
    assert len(chunks) == 4
    assert all(len(c) <= 30 for c in chunks)
    assert "".join(chunks) == text


def test_chunk_message_does_not_cut_inside_anchor_tag():
    head = "Y" * 40
    tag = '<a href="https://example.com/long/url/segments">'
    body = "Z" * 40
    text = f"{head}\n\n{tag}{body}</a>"
    chunks = chunk_message(text, max_length=60)
    # No chunk may cut in the middle of the opening tag: any chunk containing
    # "<a " must also contain that tag's closing ">".
    for chunk in chunks:
        idx = chunk.find("<a ")
        if idx != -1:
            assert ">" in chunk[idx:], f"anchor tag cut mid-tag: {chunk!r}"
    # Every chunk must be independently valid HTML: balanced <a>/</a>.
    import re as _re

    for chunk in chunks:
        opens = len(_re.findall(r"<a[ >]", chunk))
        closes = chunk.count("</a>")
        assert closes >= opens or "</a>" not in chunk


async def test_send_chunked_message_passes_kwargs_through():
    message = AsyncMock()
    await send_chunked_message(message, "tiny", parse_mode="HTML", disable_web_page_preview=True)
    message.reply_text.assert_awaited_once()
    kwargs = message.reply_text.call_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert kwargs["disable_web_page_preview"] is True
