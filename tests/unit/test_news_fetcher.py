from __future__ import annotations

import asyncio
from unittest.mock import patch

from newsdrop import news_fetcher
from newsdrop.news_fetcher import (
    _get_cache_key,
    _merge_and_dedupe,
    _truncate_text,
    fetch_breaking_news,
    fetch_top_headlines,
    format_search_results,
)
from newsdrop.state import (
    api_request_consume,
    cache_set,
)


def test_generate_summary_truncates():
    long_description = "A" * 500
    article = {"description": long_description, "content": ""}

    summary = _truncate_text(
        str(article.get("description", "") or article.get("content", "") or ""), 200
    )

    assert len(summary) <= 200, f"summary length {len(summary)} exceeds max_length=200"
    assert summary.endswith("..."), f"expected '...' suffix, got: {summary!r}"


def test_generate_summary_short_text_unchanged():
    article = {"description": "Short text.", "content": ""}
    assert (
        _truncate_text(str(article.get("description", "") or article.get("content", "") or ""), 200)
        == "Short text."
    )


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

    assert "&lt;script&gt;" in output, f"expected escaped <script> in output, got: {output!r}"
    assert "<script>" not in output, f"raw <script> tag leaked into output: {output!r}"


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
    async def _run():
        limit = news_fetcher._request_limit
        for _ in range(limit):
            await api_request_consume(limit)

        with patch("newsdrop.news_fetcher.httpx.AsyncClient") as mock_client:
            result = await fetch_breaking_news(["us"], ["war"])

        assert result == [], f"expected [] when budget exhausted, got {result!r}"
        mock_client.assert_not_called()

    asyncio.run(_run())


def test_word_boundary_keyword_matcher_rejects_substring_FPs():
    params = {
        "apikey": "x",
        "country": "us",
        "language": "en",
        "size": 10,
    }
    cache_key = _get_cache_key(params)

    async def _run_false_positive():
        await cache_set(
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
            300,
        )
        with patch("newsdrop.news_fetcher._safe_fetch_rss", return_value=[]):
            return await fetch_breaking_news(["us"], ["war"])

    matched = asyncio.run(_run_false_positive())
    assert matched == [], (
        f"expected 'war' to NOT match 'Star Wars', got false positive: {matched!r}"
    )

    async def _run_true_positive():
        await cache_set(
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
            300,
        )
        with patch("newsdrop.news_fetcher._safe_fetch_rss", return_value=[]):
            return await fetch_breaking_news(["us"], ["war"])

    matched = asyncio.run(_run_true_positive())
    assert len(matched) == 1, f"expected 'war' to match 'war declared', got {matched!r}"


def test_fetch_top_headlines_applies_reddit_cross_verification():
    async def _run():
        api_payload = {
            "status": "success",
            "results": [
                {
                    "title": "Verified headline",
                    "link": "https://example.com/verified",
                    "description": "Details",
                    "pubDate": "2025-01-02T00:00:00Z",
                    "source_name": "ExampleNews",
                },
                {
                    "title": "Regular headline",
                    "link": "https://example.com/regular",
                    "description": "Other details",
                    "pubDate": "2025-01-03T00:00:00Z",
                    "source_name": "ExampleNews",
                },
            ],
        }
        reddit_posts = [
            {
                "title": "Verified headline",
                "url": "https://example.com/verified",
                "redditSubreddit": "news",
                "redditScore": 300,
                "redditPermalink": "https://www.reddit.com/r/news/comments/abc",
            }
        ]

        with (
            patch("newsdrop.news_fetcher.ENABLE_REDDIT", True),
            patch(
                "newsdrop.news_fetcher._fetch_news",
                return_value=news_fetcher._normalize_response(api_payload),
            ),
            patch("newsdrop.news_fetcher._safe_fetch_rss", return_value=[]),
            patch("newsdrop.news_fetcher._safe_fetch_reddit", return_value=reddit_posts),
        ):
            result = await fetch_top_headlines("us", "general")

        articles = result["articles"]
        assert articles[0]["crossConfirmed"] is True
        assert articles[0]["url"] == "https://example.com/verified"
        assert "reddit" in result["sources"]

    asyncio.run(_run())
