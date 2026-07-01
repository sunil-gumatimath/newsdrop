"""Tests for rss_feeds.py — RSS parsing, image extraction, date parsing, and fetching.

Tests the pure helper functions directly (``_strip_html``, ``_parse_rss_date``,
``_extract_image``, ``_entry_to_article``) and mock ``feedparser.parse`` for the
async ``fetch_rss_articles`` flow.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from newsdrop import rss_feeds


# ── _strip_html ─────────────────────────────────────────────────────────


class TestStripHtml:
    def test_removes_simple_tags(self):
        assert rss_feeds._strip_html("<b>bold</b>") == "bold"

    def test_replaces_br_with_space(self):
        assert rss_feeds._strip_html("line1<br/>line2") == "line1 line2"

    def test_replaces_closing_p_with_space(self):
        assert rss_feeds._strip_html("para1</p>para2") == "para1 para2"

    def test_unescapes_html_entities(self):
        assert rss_feeds._strip_html("foo &amp; bar") == "foo & bar"

    def test_collapses_whitespace(self):
        assert rss_feeds._strip_html("  hello   world  ") == "hello world"

    def test_returns_empty_for_none(self):
        assert rss_feeds._strip_html(None) == ""

    def test_returns_empty_for_empty_string(self):
        assert rss_feeds._strip_html("") == ""

    def test_handles_nested_tags(self):
        result = rss_feeds._strip_html("<div><p>Hello <b>world</b></p></div>")
        assert result == "Hello world"

    def test_handles_real_rss_summary(self):
        html = '<p>Scientists discover <a href="...">new species</a> in the <b>Amazon rainforest</b>.</p>'
        result = rss_feeds._strip_html(html)
        assert "<" not in result
        assert "new species" in result
        assert "Amazon rainforest" in result


# ── _parse_rss_date ─────────────────────────────────────────────────────


class TestParseRssDate:
    def test_published_parsed_struct_time(self):
        """feedparser exposes published_parsed as a time.struct_time."""
        import time
        from datetime import UTC

        entry = {
            "published_parsed": time.struct_time((2025, 3, 15, 12, 0, 0, 0, 0, 0))
        }
        result = rss_feeds._parse_rss_date(entry)
        assert "2025-03-15" in result
        assert "12:00" in result

    def test_updated_parsed_fallback(self):
        """If published_parsed is missing, falls back to updated_parsed."""
        import time

        entry = {
            "updated_parsed": time.struct_time((2025, 6, 1, 8, 30, 0, 0, 0, 0))
        }
        result = rss_feeds._parse_rss_date(entry)
        assert "2025-06-01" in result

    def test_raw_string_fallback(self):
        """Falls back to raw published string and parses it with email.utils."""
        entry = {"published": "Sat, 15 Mar 2025 12:00:00 GMT"}
        result = rss_feeds._parse_rss_date(entry)
        assert "2025-03-15" in result

    def test_returns_empty_for_no_date(self):
        entry: dict = {}
        assert rss_feeds._parse_rss_date(entry) == ""

    def test_returns_empty_for_unparseable(self):
        entry = {"published": "not-a-date-at-all-xyz"}
        assert rss_feeds._parse_rss_date(entry) == ""


# ── _extract_image ──────────────────────────────────────────────────────


class TestExtractImage:
    def test_media_content_url(self):
        entry = {
            "media_content": [{"url": "https://cdn.example.com/image.jpg"}]
        }
        assert rss_feeds._extract_image(entry) == "https://cdn.example.com/image.jpg"

    def test_media_thumbnail_url(self):
        entry = {
            "media_thumbnail": [{"url": "https://cdn.example.com/thumb.jpg"}]
        }
        assert rss_feeds._extract_image(entry) == "https://cdn.example.com/thumb.jpg"

    def test_enclosure_image(self):
        entry = {
            "enclosures": [
                {"type": "image/jpeg", "href": "https://cdn.example.com/photo.jpg"}
            ]
        }
        assert rss_feeds._extract_image(entry) == "https://cdn.example.com/photo.jpg"

    def test_img_tag_in_summary(self):
        entry = {
            "summary": '<p>Read more</p><img src="https://cdn.example.com/img.png" />'
        }
        assert rss_feeds._extract_image(entry) == "https://cdn.example.com/img.png"

    def test_returns_empty_when_no_image(self):
        entry: dict = {"summary": "Just text, no images."}
        assert rss_feeds._extract_image(entry) == ""

    def test_media_content_takes_priority_over_thumbnail(self):
        entry = {
            "media_content": [{"url": "https://cdn.example.com/main.jpg"}],
            "media_thumbnail": [{"url": "https://cdn.example.com/thumb.jpg"}],
        }
        assert rss_feeds._extract_image(entry) == "https://cdn.example.com/main.jpg"

    def test_skips_non_image_enclosures(self):
        entry = {
            "enclosures": [
                {"type": "audio/mpeg", "href": "https://cdn.example.com/audio.mp3"},
                {"type": "image/png", "href": "https://cdn.example.com/img.png"},
            ]
        }
        assert rss_feeds._extract_image(entry) == "https://cdn.example.com/img.png"


# ── _entry_to_article ───────────────────────────────────────────────────


class TestEntryToArticle:
    def test_basic_conversion(self):
        entry = {
            "title": "Test Headline",
            "link": "https://example.com/article",
            "summary": "<p>A <b>great</b> article.</p>",
            "published_parsed": __import__("time").struct_time(
                (2025, 1, 15, 10, 0, 0, 0, 0, 0)
            ),
        }
        result = rss_feeds._entry_to_article(entry, "TestSource")

        assert result["title"] == "Test Headline"
        assert result["url"] == "https://example.com/article"
        assert result["description"] == "A great article."
        assert result["source"]["name"] == "TestSource"
        assert "2025-01-15" in result["publishedAt"]

    def test_defaults_for_missing_fields(self):
        entry: dict = {}
        result = rss_feeds._entry_to_article(entry, "Unknown")

        assert result["title"] == "No title"
        assert result["url"] == ""
        assert result["description"] == ""
        assert result["urlToImage"] == ""
        assert result["publishedAt"] == ""

    def test_truncates_long_description(self):
        entry = {"summary": "x" * 1000}
        result = rss_feeds._entry_to_article(entry, "Source")
        assert len(result["description"]) <= 500
        assert result["description"].endswith("...")

    def test_content_equals_description(self):
        entry = {"summary": "Short summary"}
        result = rss_feeds._entry_to_article(entry, "Source")
        assert result["content"] == result["description"]

    def test_strips_html_from_title(self):
        entry = {"title": "<b>HTML Title</b>"}
        result = rss_feeds._entry_to_article(entry, "Source")
        assert result["title"] == "HTML Title"


# ── fetch_rss_articles (async, mocked feedparser) ───────────────────────


@pytest.mark.asyncio
async def test_fetch_rss_articles_no_feeds_for_country():
    """Returns empty list when no RSS feeds are configured for the country."""
    result = await rss_feeds.fetch_rss_articles("xx")
    assert result == []


@pytest.mark.asyncio
async def test_fetch_rss_articles_returns_sorted(tmp_path=None):
    """Articles are returned sorted by publishedAt descending."""
    fake_entry_new = {
        "title": "New Article",
        "link": "https://example.com/new",
        "summary": "Newest article",
        "published_parsed": __import__("time").struct_time(
            (2025, 6, 1, 12, 0, 0, 0, 0, 0)
        ),
    }
    fake_entry_old = {
        "title": "Old Article",
        "link": "https://example.com/old",
        "summary": "Older article",
        "published_parsed": __import__("time").struct_time(
            (2025, 1, 1, 12, 0, 0, 0, 0, 0)
        ),
    }

    # Use 'au' which has only 1 RSS feed so _fetch_feed is called once
    with patch("newsdrop.rss_feeds._fetch_feed", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = [
            rss_feeds._entry_to_article(e, "TestSource")
            for e in [fake_entry_old, fake_entry_new]
        ]
        result = await rss_feeds.fetch_rss_articles("au", limit=30)

    # Newest first
    assert len(result) == 2
    assert "2025-06-01" in result[0]["publishedAt"]
    assert "2025-01-01" in result[1]["publishedAt"]


@pytest.mark.asyncio
async def test_fetch_rss_articles_respects_limit():
    """The limit parameter caps the number of returned articles."""
    fake_entries = [
        {
            "title": f"Article {i}",
            "link": f"https://example.com/{i}",
            "summary": f"Summary {i}",
            "published_parsed": __import__("time").struct_time(
                (2025, 6, 1 + (i % 28), 12, 0, 0, 0, 0, 0)
            ),
        }
        for i in range(50)
    ]

    with patch("newsdrop.rss_feeds._fetch_feed", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = [
            rss_feeds._entry_to_article(e, "TestSource") for e in fake_entries
        ]
        result = await rss_feeds.fetch_rss_articles("us", limit=10)

    assert len(result) == 10


@pytest.mark.asyncio
async def test_fetch_rss_articles_handles_fetch_errors(tmp_path=None):
    """If a feed fetch fails, other feeds still return results."""
    good_entry = {
        "title": "Good Article",
        "link": "https://example.com/good",
        "summary": "Works fine",
        "published_parsed": __import__("time").struct_time(
            (2025, 6, 1, 12, 0, 0, 0, 0, 0)
        ),
    }

    with patch("newsdrop.rss_feeds._fetch_feed", new_callable=AsyncMock) as mock_fetch:
        # First feed succeeds, second raises
        mock_fetch.side_effect = [
            [rss_feeds._entry_to_article(good_entry, "GoodSource")],
            Exception("Connection timeout"),
        ]
        result = await rss_feeds.fetch_rss_articles("us", limit=30)

    # Should still get results from the good feed
    assert len(result) == 1
    assert result[0]["title"] == "Good Article"


@pytest.mark.asyncio
async def test_fetch_rss_articles_empty_when_all_fail():
    """Returns empty list when all feeds fail."""
    with patch("newsdrop.rss_feeds._fetch_feed", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = Exception("Network down")
        result = await rss_feeds.fetch_rss_articles("us", limit=30)

    assert result == []


# ── has_rss_for ─────────────────────────────────────────────────────────


class TestHasRssFor:
    def test_country_with_feeds(self):
        assert rss_feeds.has_rss_for("us") is True

    def test_country_without_feeds(self):
        assert rss_feeds.has_rss_for("xx") is False

    def test_empty_string(self):
        assert rss_feeds.has_rss_for("") is False
