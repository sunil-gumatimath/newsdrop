"""Unit tests for ``news_fetcher`` (pure functions + isolated rate-limit gate).

These tests deliberately avoid any real HTTP. Where the function under test
calls ``httpx.AsyncClient`` we either pre-populate the in-memory cache or
patch ``httpx.AsyncClient`` to assert it was *not* called.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from news_fetcher import (
    _cache,
    _daily_request_count,
    _get_cache_key,
    _merge_and_dedupe,
    _set_cache,
    _request_limit,
    _truncate_text,
    fetch_breaking_news,
    format_search_results,
)


# ---------------------------------------------------------------------------
# Pure-function tests (no async, no mocks needed)
# ---------------------------------------------------------------------------


def test_generate_summary_truncates():
    """``_truncate_text`` caps at ``max_length`` and ends with "..."."""
    long_description = "A" * 500  # 500 chars, well over the 200-char default
    article = {"description": long_description, "content": ""}

    summary = _truncate_text(
        str(article.get("description", "") or article.get("content", "") or ""), 200
    )

    assert len(summary) <= 200, (
        f"summary length {len(summary)} exceeds max_length=200"
    )
    assert summary.endswith("..."), f"expected '...' suffix, got: {summary!r}"


def test_generate_summary_short_text_unchanged():
    """Short descriptions pass through untouched (no spurious ellipsis)."""
    article = {"description": "Short text.", "content": ""}
    assert _truncate_text(
        str(article.get("description", "") or article.get("content", "") or ""), 200
    ) == "Short text."


def test_format_search_results_escapes_html():
    """``<script>`` in the description must be HTML-escaped to ``&lt;script&gt;``."""
    data = {
        "articles": [
            {
                "title": "Test headline",
                "url": "https://example.com/news/1",
                "description": "<script>alert('xss')</script>",
                "source": {"name": "ExampleNews"},
                "publishedAt": "",
            }
        ],
        "totalResults": 1,
    }
    output = format_search_results(data, query="xss")

    assert "&lt;script&gt;" in output, (
        f"expected escaped <script> in output, got: {output!r}"
    )
    # And critically, the raw tag should NOT appear.
    assert "<script>" not in output, (
        f"raw <script> tag leaked into output: {output!r}"
    )


def test_merge_and_dedupe_dedupes_by_url_and_title():
    """5 articles where 2 share a URL and 2 share a normalized title → 3 unique.

    Dedup order (input is processed in order):
      * Article 0 ("First headline" / url-a)            → kept
      * Article 1 ("Second headline" / url-b)            → kept
      * Article 2 ("Different title, same URL" / url-a)  → skipped (url dup)
      * Article 3 ("second  headline!!" / url-c)         → skipped (title dup)
      * Article 4 ("Completely unique" / url-d)          → kept

    After sort by ``publishedAt`` DESC the survivors are d, b, a.
    """
    articles = [
        {
            "title": "First headline",
            "url": "https://example.com/a",
            "publishedAt": "2024-01-01T00:00:00Z",
        },
        {
            "title": "Second headline",
            "url": "https://example.com/b",
            "publishedAt": "2024-01-02T00:00:00Z",
        },
        {
            # Duplicate URL with article #0
            "title": "Different title, same URL",
            "url": "https://example.com/a",
            "publishedAt": "2024-01-03T00:00:00Z",
        },
        {
            # Duplicate normalized title with article #1 (case/punct differences)
            "title": "second  headline!!",
            "url": "https://example.com/c",
            "publishedAt": "2024-01-04T00:00:00Z",
        },
        {
            "title": "Completely unique",
            "url": "https://example.com/d",
            "publishedAt": "2024-01-05T00:00:00Z",
        },
    ]

    merged = _merge_and_dedupe(articles, limit=10)

    assert len(merged) == 3, f"expected 3 unique articles, got {len(merged)}: {merged}"
    # Output is sorted by publishedAt DESC, so the latest comes first.
    assert merged[0]["url"] == "https://example.com/d"
    assert merged[1]["url"] == "https://example.com/b"
    assert merged[2]["url"] == "https://example.com/a"


# ---------------------------------------------------------------------------
# Async tests
# ---------------------------------------------------------------------------


def test_rate_limit_gate_blocks_when_budget_exhausted():
    """When daily count == limit, ``fetch_breaking_news`` returns [] with no HTTP."""
    # Force the gate to "exhausted" without touching real time or cache state.
    # The autouse _isolate_rate_limit_state fixture resets this between tests.
    import news_fetcher

    news_fetcher._daily_request_count = 200
    news_fetcher._request_limit = 200

    # If fetch_breaking_news hits the network, this mock's constructor gets
    # called and the test fails. We never expect to enter the network path.
    with patch("news_fetcher.httpx.AsyncClient") as mock_client:
        result = asyncio.run(fetch_breaking_news(["us"], ["war"]))

    assert result == [], f"expected [] when budget exhausted, got {result!r}"
    mock_client.assert_not_called(), (
        "httpx.AsyncClient was constructed even though the rate-limit gate "
        "should have short-circuited the call"
    )


def test_word_boundary_keyword_matcher_rejects_substring_FPs():
    """Word-boundary regex prevents substring false positives.

    Confirmed implementation (news_fetcher.py:745):
        re.search(rf"\\b{re.escape(kw.lower())}\\b", combined)

    Behavior verified by this test (note: this is *stricter* than the
    example claim in the production comment at lines 738-741 — the
    ``\\b`` anchors mean "war" does NOT match inside "Wars" because the
    'r' is immediately followed by 's', breaking the right boundary):

      * "war" does NOT match "Star Wars"  (r→s breaks the right boundary)
      * "war" DOES    match "war declared" (whole word, both boundaries)
    """
    import news_fetcher

    # Make sure budget is available so we don't trip the rate-limit gate.
    news_fetcher._daily_request_count = 0
    news_fetcher._request_limit = 200

    params = {
        "apikey": "x",
        "country": "us",
        "language": "en",
        "size": 10,
    }
    cache_key = _get_cache_key(params)

    # --- Case 1: "war" should NOT match "Star Wars" (substring FP, boundary fails)
    _cache.clear()
    _set_cache(
        cache_key,
        {
            "articles": [
                {
                    "title": "Star Wars announces new film",
                    "description": "The franchise returns.",
                    "url": "https://example.com/sw",
                }
            ]
        },
    )
    matched = asyncio.run(fetch_breaking_news(["us"], ["war"]))
    assert matched == [], (
        f"expected 'war' to NOT match 'Star Wars' (r is followed by s, no "
        f"word boundary), got false positive: {matched!r}"
    )

    # --- Case 2: "war" SHOULD match "war declared" (whole-word match)
    _cache.clear()
    _set_cache(
        cache_key,
        {
            "articles": [
                {
                    "title": "war declared in the region",
                    "description": "Tensions are rising.",
                    "url": "https://example.com/war2",
                }
            ]
        },
    )
    matched = asyncio.run(fetch_breaking_news(["us"], ["war"]))
    assert len(matched) == 1, (
        f"expected 'war' to match 'war declared' (whole word, both "
        f"boundaries), got {matched!r}"
    )
