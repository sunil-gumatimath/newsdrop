"""Additional unit tests for rss_feeds.py: date parsing, health cooldown, catalog lookups."""

from __future__ import annotations

import time
from unittest.mock import patch

from newsdrop import rss_feeds

# ── _parse_rss_date edge cases ──────────────────────────────────────────


class TestParseRssDateEdgeCases:
    def test_broken_struct_time_falls_back_to_raw(self):
        """A malformed struct falls through to the raw string path."""
        entry = {
            "published_parsed": (2025, 3),  # too short → datetime() raises
            "published": "Sat, 15 Mar 2025 12:00:00 GMT",
        }
        result = rss_feeds._parse_rss_date(entry)
        assert "2025-03-15" in result

    def test_updated_raw_string_fallback(self):
        entry = {"updated": "Sun, 01 Jun 2025 08:30:00 +0200"}
        result = rss_feeds._parse_rss_date(entry)
        assert "2025-06-01" in result

    def test_unparseable_raw_string_returns_empty(self):
        entry = {"published": "definitely not a timestamp", "updated": ""}
        assert rss_feeds._parse_rss_date(entry) == ""

    def test_empty_entry_returns_empty(self):
        assert rss_feeds._parse_rss_date({}) == ""

    def test_parsed_date_is_iso_utc(self):
        entry = {"published_parsed": time.struct_time((2025, 1, 2, 3, 4, 5, 0, 0, 0))}
        result = rss_feeds._parse_rss_date(entry)
        assert result == "2025-01-02T03:04:05+00:00"


# ── feed health cooldown via monkeypatched time.monotonic ───────────────


class TestFeedCooldownExpiry:
    def test_cooldown_expires_after_monotonic_advance(self):
        rss_feeds.reset_feed_health()
        url = "https://example.com/cooldown.xml"
        fake_now = 1000.0
        with patch.object(rss_feeds.time, "monotonic", return_value=fake_now):
            for _ in range(rss_feeds._FEED_FAILURE_THRESHOLD):
                rss_feeds._record_feed_failure(url)
            # Still inside the cooldown window.
            with patch.object(rss_feeds.time, "monotonic", return_value=fake_now + 1):
                assert rss_feeds._feed_is_available(url) is False
            # After the cooldown elapses the feed is available again and its
            # failure counter was reset.
            with patch.object(
                rss_feeds.time,
                "monotonic",
                return_value=fake_now + rss_feeds._FEED_COOLDOWN_SECONDS,
            ):
                assert rss_feeds._feed_is_available(url) is True
        assert url not in rss_feeds._feed_failures
        assert url not in rss_feeds._feed_disabled_until
        rss_feeds.reset_feed_health()

    def test_failures_below_threshold_do_not_disable(self):
        rss_feeds.reset_feed_health()
        url = "https://example.com/flaky.xml"
        for _ in range(rss_feeds._FEED_FAILURE_THRESHOLD - 1):
            rss_feeds._record_feed_failure(url)
        assert rss_feeds._feed_is_available(url) is True
        assert rss_feeds._feed_failures[url] == rss_feeds._FEED_FAILURE_THRESHOLD - 1
        rss_feeds.reset_feed_health()

    def test_unknown_feed_is_available(self):
        rss_feeds.reset_feed_health()
        assert rss_feeds._feed_is_available("https://example.com/never-seen.xml") is True


# ── catalog lookups ─────────────────────────────────────────────────────


class TestCatalogLookups:
    def test_has_rss_for_normalizes_category_case(self):
        assert rss_feeds.has_rss_for("xx", category=" TECHNOLOGY ") is True

    def test_has_rss_for_general_requires_country(self):
        assert rss_feeds.has_rss_for("zz", category="general") is False

    def test_has_rss_category_unknown_and_empty(self):
        assert rss_feeds.has_rss_category("nosuchcategory") is False
        assert rss_feeds.has_rss_category("") is False

    def test_fetch_rss_category_articles_unknown_category(self):
        import asyncio

        result = asyncio.run(rss_feeds.fetch_rss_category_articles("nope"))
        assert result == []


# ── _entry_body_text ────────────────────────────────────────────────────


class TestEntryBodyText:
    def test_prefers_summary_over_content(self):
        entry = {
            "summary": "A perfectly adequate summary text here.",
            "content": [{"value": "Content encoded body that also has length."}],
        }
        body = rss_feeds._entry_body_text(entry)
        assert "summary" in body

    def test_falls_back_to_content_list(self):
        entry = {"content": [{"value": "Long enough content-encoded value for the body."}]}
        body = rss_feeds._entry_body_text(entry)
        assert "content-encoded" in body

    def test_short_candidates_return_stripped_first(self):
        entry = {"summary": "<b>tiny</b>"}
        assert rss_feeds._entry_body_text(entry) == "tiny"

    def test_no_body_returns_empty(self):
        assert rss_feeds._entry_body_text({}) == ""
