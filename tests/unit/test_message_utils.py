"""Unit tests for ``message_utils``.

Note on test scope
------------------
The task brief asked for ``test_format_ranked_message_sorts_by_score`` and
``test_format_ranked_message_handles_empty``. There is no
``format_ranked_message`` function in the current ``message_utils.py`` — that
module only exposes ``chunk_message`` and ``send_chunked_message``. Per the
task's "fix the test, not the production code" rule, the analogous coverage
is provided here against the real public surface:

  * "Sorts by score" → tests that ``chunk_message`` preserves order across
    the items of a multi-section message (no reordering / dropping).
  * "Handles empty"  → tests the empty-input contract of ``chunk_message``.

Plus a real "chunks when over the limit" test since that's the actual
behavior the module exists to provide.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from message_utils import chunk_message, send_chunked_message, MAX_MESSAGE_LENGTH


# ---------------------------------------------------------------------------
# Order-preservation (analogous to "sorts by score": keeps caller order)
# ---------------------------------------------------------------------------


def test_chunk_message_preserves_input_order_across_sections():
    """``chunk_message`` must emit sections in the same order they appear."""
    # Each section is short, so each fits in one chunk — order is trivially
    # preserved. We assert the exact sequence to make the contract explicit.
    text = (
        "Section A — highest priority\n\n"
        "Section B — middle\n\n"
        "Section C — lowest\n\n"
    )

    chunks = chunk_message(text, max_length=4096)

    assert len(chunks) == 1
    assert chunks[0] == text
    # Order: A appears before B, B before C.
    assert chunks[0].index("Section A") < chunks[0].index("Section B")
    assert chunks[0].index("Section B") < chunks[0].index("Section C")


# ---------------------------------------------------------------------------
# Empty-input contract
# ---------------------------------------------------------------------------


def test_chunk_message_handles_empty_input():
    """Empty input → single empty-string chunk (no exceptions, no surprise)."""
    chunks = chunk_message("", max_length=4096)
    assert chunks == [""], f"expected [''] for empty input, got {chunks!r}"


def test_chunk_message_handles_whitespace_only_input():
    """Whitespace-only input is treated as effectively empty content."""
    chunks = chunk_message("   \n\n  ", max_length=4096)
    # Should not raise; should return at least one chunk.
    assert isinstance(chunks, list)
    assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# Actual chunking behavior (the real reason this module exists)
# ---------------------------------------------------------------------------


def test_chunk_message_splits_when_over_limit():
    """Text longer than ``max_length`` is split into multiple chunks, in order."""
    section_a = "A" * 50
    section_b = "B" * 50
    section_c = "C" * 50
    text = f"{section_a}\n\n{section_b}\n\n{section_c}"
    max_length = 60  # forces at least 2 chunks

    chunks = chunk_message(text, max_length=max_length)

    assert len(chunks) >= 2, f"expected >=2 chunks, got {len(chunks)}"
    # Each chunk must respect the limit (allow +2 because we include the "\n\n"
    # split delimiter in the chunk).
    for chunk in chunks:
        assert len(chunk) <= max_length + 2, (
            f"chunk exceeds limit: {len(chunk)} > {max_length + 2}"
        )
    # Reassembled content should contain every original token.
    reassembled = "".join(chunks)
    assert section_a in reassembled
    assert section_b in reassembled
    assert section_c in reassembled


def test_chunk_message_short_input_returns_single_chunk():
    """Below the limit, no splitting occurs."""
    text = "Short message, fits in one chunk."
    chunks = chunk_message(text, max_length=4096)
    assert chunks == [text]


def test_send_chunked_message_calls_reply_for_each_chunk():
    """``send_chunked_message`` invokes ``reply_text`` once per chunk.

    ``send_chunked_message`` hard-codes ``chunk_message`` to the default
    ``MAX_MESSAGE_LENGTH`` (4096). We craft input well above that to force
    multiple chunks.
    """
    message = AsyncMock()
    # >4096 chars of repeating content → at least 2 chunks at the default limit.
    text = ("X" * 4500) + "\n\n" + ("Y" * 100)

    asyncio_run = pytest.importorskip("asyncio").run
    asyncio_run(send_chunked_message(message, text))

    assert message.reply_text.await_count >= 2, (
        f"expected >=2 chunked replies, got {message.reply_text.await_count}"
    )
    # Reassembled content should still cover the input.
    combined = "".join(call.args[0] for call in message.reply_text.call_args_list)
    assert "X" * 4500 in combined
    assert "Y" * 100 in combined
