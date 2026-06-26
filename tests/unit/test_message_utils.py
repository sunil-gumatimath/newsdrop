from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from newsdrop.message_utils import chunk_message, send_chunked_message


def test_chunk_message_preserves_input_order_across_sections():
    text = (
        "Section A \u2014 highest priority\n\n"
        "Section B \u2014 middle\n\n"
        "Section C \u2014 lowest\n\n"
    )

    chunks = chunk_message(text, max_length=4096)

    assert len(chunks) == 1
    assert chunks[0] == text
    assert chunks[0].index("Section A") < chunks[0].index("Section B")
    assert chunks[0].index("Section B") < chunks[0].index("Section C")


def test_chunk_message_handles_empty_input():
    chunks = chunk_message("", max_length=4096)
    assert chunks == [""], f"expected [''] for empty input, got {chunks!r}"


def test_chunk_message_handles_whitespace_only_input():
    chunks = chunk_message("   \n\n  ", max_length=4096)
    assert isinstance(chunks, list)
    assert len(chunks) >= 1


def test_chunk_message_splits_when_over_limit():
    section_a = "A" * 50
    section_b = "B" * 50
    section_c = "C" * 50
    text = f"{section_a}\n\n{section_b}\n\n{section_c}"
    max_length = 60

    chunks = chunk_message(text, max_length=max_length)

    assert len(chunks) >= 2, f"expected >=2 chunks, got {len(chunks)}"
    for chunk in chunks:
        assert len(chunk) <= max_length + 2, (
            f"chunk exceeds limit: {len(chunk)} > {max_length + 2}"
        )
    reassembled = "".join(chunks)
    assert section_a in reassembled
    assert section_b in reassembled
    assert section_c in reassembled


def test_chunk_message_short_input_returns_single_chunk():
    text = "Short message, fits in one chunk."
    chunks = chunk_message(text, max_length=4096)
    assert chunks == [text]


def test_send_chunked_message_calls_reply_for_each_chunk():
    message = AsyncMock()
    text = ("X" * 4500) + "\n\n" + ("Y" * 100)

    asyncio_run = pytest.importorskip("asyncio").run
    asyncio_run(send_chunked_message(message, text))

    assert message.reply_text.await_count >= 2, (
        f"expected >=2 chunked replies, got {message.reply_text.await_count}"
    )
    combined = "".join(call.args[0] for call in message.reply_text.call_args_list)
    assert "X" * 4500 in combined
    assert "Y" * 100 in combined
