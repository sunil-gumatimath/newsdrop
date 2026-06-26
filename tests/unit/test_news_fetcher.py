from __future__ import annotations

import asyncio
from unittest.mock import patch

from newsdrop import news_fetcher
from newsdrop.news_fetcher import (
    _cache,
    _get_cache_key,
    _merge_and_dedupe,
    _set_cache,
    _truncate_text,
    fetch_breaking_news,
    format_search_results,
)


def test_generate_summary_truncates():
    long_description = "A" * 500
    article = {"description": long_description, "content": ""}

    summary = _truncate_text(
        str(article.get("description", "") or article.get("content", "") or ""), 200
    )

    assert len(summary) <= 200, (
        f"summary length {len(summary)} exceeds max_length=200"
    )
    assert summary.endswith("..."), f"expected '...' suffix, got: {summary!r}"


def test_generate_summary_short_text_unchanged():
    article = {"description": "Short text.", "content": ""}
    assert _truncate_text(
        str(article.get("description", "") or article.get("content", "") or ""), 200
    ) == "Short text."


def test_format_search_results_escapes_html():
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
    assert "<script>" not in output, (
        f"raw <script> tag leaked into output: {output!r}"
    )


def test_merge_and_dedupe_dedupes_by_url_and_title():
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
            "title": "Different title, same URL",
            "url": "https://example.com/a",
            "publishedAt": "2024-01-03T00:00:00Z",
        },
        {
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
    assert merged[0]["url"] == "https://example.com/d"
    assert merged[1]["url"] == "https://example.com/b"
    assert merged[2]["url"] == "https://example.com/a"


def test_rate_limit_gate_blocks_when_budget_exhausted():
    news_fetcher._daily_request_count = 200
    news_fetcher._request_limit = 200

    with patch("newsdrop.news_fetcher.httpx.AsyncClient") as mock_client:
        result = asyncio.run(fetch_breaking_news(["us"], ["war"]))

    assert result == [], f"expected [] when budget exhausted, got {result!r}"
    mock_client.assert_not_called()


def test_word_boundary_keyword_matcher_rejects_substring_FPs():
    news_fetcher._daily_request_count = 0
    news_fetcher._request_limit = 200

    params = {
        "apikey": "x",
        "country": "us",
        "language": "en",
        "size": 10,
    }
    cache_key = _get_cache_key(params)

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
        f"expected 'war' to NOT match 'Star Wars', got false positive: {matched!r}"
    )

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
        f"expected 'war' to match 'war declared', got {matched!r}"
    )
