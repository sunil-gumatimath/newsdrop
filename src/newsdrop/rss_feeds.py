"""RSS feed source for multi-source news aggregation.

Provides a curated catalog of RSS feeds per country / category and an async
fetcher that returns articles in the same NewsAPI-style shape used elsewhere
in the project (so they can be merged seamlessly with NewsData.io results).
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import os
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import feedparser
import httpx

logger = logging.getLogger(__name__)

# Skip a feed after this many consecutive failures, then re-try after cooldown.
_FEED_FAILURE_THRESHOLD = 3
_FEED_COOLDOWN_SECONDS = 3600  # 1 hour
_feed_failures: dict[str, int] = {}
_feed_disabled_until: dict[str, float] = {}

# Curated RSS feed catalog.  Keys are ISO 3166-1 alpha-2 country codes to
# match the NewsData.io `country` param used elsewhere.  Each feed is:
#   (source_display_name, url)
RSS_FEEDS: dict[str, list[tuple[str, str]]] = {
    "world": [
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("The Guardian World", "https://www.theguardian.com/world/rss"),
        ("Reuters", "https://openrss.org/feed/www.reuters.com"),
        ("Associated Press", "https://openrss.org/feed/apnews.com"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
    ],
    "in": [
        ("News on AIR", "https://newsonair.gov.in/category/national/feed/"),
        ("Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"),
        ("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss"),
        ("NDTV", "https://feeds.feedburner.com/ndtvnews-top-stories"),
        ("Indian Express", "https://indianexpress.com/section/india/feed/"),
        ("Hindustan Times", "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml"),
        ("Deccan Herald", "https://www.deccanherald.com/stories.rss"),
        ("The Quint", "https://www.thequint.com/feed"),
        ("BBC News India", "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml"),
    ],
    "us": [
        ("NPR", "https://feeds.npr.org/1001/rss.xml"),
        ("BBC (US & Canada)", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"),
        ("NYT World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
        ("Reuters", "https://openrss.org/feed/www.reuters.com"),
        ("Associated Press", "https://openrss.org/feed/apnews.com"),
        ("FactCheck.org", "https://www.factcheck.org/feed/"),
        ("Bloomberg", "https://openrss.org/feed/www.bloomberg.com"),
    ],
    "gb": [
        ("BBC News", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/uk/rss"),
        ("Sky News", "https://feeds.skynews.com/feeds/rss/uk.xml"),
    ],
    "au": [
        ("ABC News", "https://www.abc.net.au/news/feed/51120/rss.xml"),
    ],
    "de": [
        ("Deutsche Welle", "https://rss.dw.com/rdf/rss-en-all"),
    ],
    "fr": [
        ("France 24", "https://www.france24.com/en/rss"),
    ],
    "jp": [
        ("Japan Times", "https://www.japantimes.co.jp/feed/"),
    ],
}

# Category-specific feeds. Used when the user picks a non-general category so
# we don't have to rely on brittle keyword filtering of general headlines.
RSS_CATEGORY_FEEDS: dict[str, list[tuple[str, str]]] = {
    "technology": [
        ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
        ("Wired", "https://www.wired.com/feed/rss"),
        ("TechCrunch", "https://techcrunch.com/feed/"),
        ("NPR Technology", "https://feeds.npr.org/1019/rss.xml"),
    ],
    "business": [
        ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        (
            "CNBC",
            "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",
        ),
        ("NPR Business", "https://feeds.npr.org/1006/rss.xml"),
        ("The Guardian Business", "https://www.theguardian.com/uk/business/rss"),
        ("Reuters Business", "https://openrss.org/feed/www.reuters.com/business"),
        ("News on AIR Business", "https://newsonair.gov.in/category/business/feed/"),
    ],
    "sports": [
        ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
        ("ESPN", "https://www.espn.com/espn/rss/news"),
        ("The Guardian Sport", "https://www.theguardian.com/uk/sport/rss"),
        ("NPR Sports", "https://feeds.npr.org/1055/rss.xml"),
        ("News on AIR Sports", "https://newsonair.gov.in/category/sports/feed/"),
    ],
    "entertainment": [
        ("BBC Entertainment", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
        ("Variety", "https://variety.com/feed/"),
        ("The Guardian Culture", "https://www.theguardian.com/uk/culture/rss"),
        ("NPR Movies", "https://feeds.npr.org/1045/rss.xml"),
    ],
    "health": [
        ("BBC Health", "https://feeds.bbci.co.uk/news/health/rss.xml"),
        ("NPR Health", "https://feeds.npr.org/1128/rss.xml"),
        ("The Guardian Society", "https://www.theguardian.com/society/rss"),
        ("WHO News", "https://www.who.int/rss-feeds/news-english.xml"),
    ],
    "science": [
        ("BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
        ("NASA", "https://www.nasa.gov/rss/dyn/breaking_news.rss"),
        ("ScienceDaily", "https://www.sciencedaily.com/rss/all.xml"),
        ("NPR Science", "https://feeds.npr.org/1007/rss.xml"),
        ("The Guardian Science", "https://www.theguardian.com/science/rss"),
    ],
}


def reset_feed_health() -> None:
    """Clear feed health state (for tests)."""
    _feed_failures.clear()
    _feed_disabled_until.clear()


def _feed_is_available(url: str) -> bool:
    until = _feed_disabled_until.get(url, 0.0)
    if until <= 0:
        return True
    if time.monotonic() >= until:
        _feed_disabled_until.pop(url, None)
        _feed_failures.pop(url, None)
        return True
    return False


def _record_feed_success(url: str) -> None:
    _feed_failures.pop(url, None)
    _feed_disabled_until.pop(url, None)


def _record_feed_failure(url: str) -> None:
    count = _feed_failures.get(url, 0) + 1
    _feed_failures[url] = count
    if count >= _FEED_FAILURE_THRESHOLD:
        _feed_disabled_until[url] = time.monotonic() + _FEED_COOLDOWN_SECONDS
        logger.warning(
            "RSS feed %s disabled for %ss after %s consecutive failures",
            url,
            _FEED_COOLDOWN_SECONDS,
            count,
        )


def _strip_html(text: str | None) -> str:
    """Remove HTML tags from a string (RSS summaries often contain markup)."""
    if text is None or text == "":
        return ""
    if not text:
        return ""
    text = str(text)
    # Replace <br> and </p> with spaces before stripping
    text = re.sub(r"<br\s*/?>|</p>", " ", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape HTML entities
    text = html_lib.unescape(text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_rss_date(entry: dict) -> str:
    """Extract publish date from a feedparser entry and return it as an ISO 8601 UTC string.

    Returns an empty string if no parseable date is found.
    """
    # feedparser exposes parsed time in published_parsed / updated_parsed
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                dt = datetime(*t[:6], tzinfo=UTC)  # type: ignore[misc]
                return str(dt.isoformat())
            except Exception:
                pass

    # Fallback: raw string
    raw = entry.get("published") or entry.get("updated") or ""
    if raw:
        try:
            dt_raw: datetime = parsedate_to_datetime(raw)
            if dt_raw.tzinfo is None:
                dt_raw = dt_raw.replace(tzinfo=UTC)
            return str(dt_raw.isoformat())
        except Exception:
            pass
    return ""


def _extract_image(entry: dict) -> str:
    """Try a few common RSS image locations."""
    # <media:content url="..."/>
    media = entry.get("media_content") or []
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return str(url)
    # <media:thumbnail url="..."/>
    thumbs = entry.get("media_thumbnail") or []
    if thumbs and isinstance(thumbs, list):
        url = thumbs[0].get("url")
        if url:
            return str(url)
    # <enclosure type="image/...">
    for enc in entry.get("enclosures", []) or []:
        if enc.get("type", "").startswith("image/") and enc.get("href"):
            return str(enc["href"])
    # image in summary/content — handle both single and double quoted src
    summary = entry.get("summary") or ""
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def _entry_body_text(entry: dict) -> str:
    """Best available plain-text blurb from a feedparser entry."""
    # Prefer summary/description; fall back to content:encoded bodies.
    candidates: list[str] = []
    for key in ("summary", "description"):
        raw = entry.get(key)
        if raw:
            candidates.append(str(raw))

    content = entry.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("value"):
                candidates.append(str(block["value"]))
    elif isinstance(content, str) and content:
        candidates.append(content)

    for raw in candidates:
        text = _strip_html(raw)
        if len(text) >= 20:
            if len(text) > 500:
                text = text[:497].rsplit(" ", 1)[0] + "..."
            return text
    return _strip_html(candidates[0]) if candidates else ""


def _safe_url(url: object) -> str:
    """Return a validated http(s) URL, or "" if invalid/missing."""
    if not isinstance(url, str):
        return ""
    candidate = url.strip()
    if not candidate:
        return ""
    try:
        parsed = urlparse(candidate)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return candidate


def _entry_to_article(entry: dict, source_name: str) -> dict:
    """Convert a feedparser entry into the NewsAPI-style shape used by bot.py."""
    title = _strip_html(entry.get("title", "")) or "No title"
    description = _entry_body_text(entry)

    return {
        "title": title,
        "url": _safe_url(entry.get("link", "") or ""),
        "description": description,
        "content": description,
        "urlToImage": _safe_url(_extract_image(entry)),
        "publishedAt": _parse_rss_date(entry),
        "source": {"name": source_name},
    }


async def _fetch_feed(client: httpx.AsyncClient, source_name: str, url: str) -> list[dict]:
    """Fetch and parse a single RSS feed. Returns [] on any failure."""
    if not _feed_is_available(url):
        logger.debug("Skipping RSS feed %s (temporarily disabled)", url)
        return []

    try:
        async with client.stream("GET", url, follow_redirects=True) as r:
            if r.status_code != 200:
                logger.warning("RSS feed %s returned HTTP %s", url, r.status_code)
                _record_feed_failure(url)
                return []
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) >= 2_000_000:
                    logger.warning("RSS feed %s response too large (>=2MB), aborting", url)
                    raise ValueError("Response too large")
        # feedparser is synchronous but parses bytes quickly; offload to thread.
        if b"<!ENTITY" in body.lower():
            logger.warning("RSS feed %s rejected: XML entity declarations found", url)
            _record_feed_failure(url)
            return []
        parsed = await asyncio.to_thread(feedparser.parse, body)
        if parsed.bozo and not parsed.entries:
            logger.warning("RSS feed %s failed to parse: %s", url, parsed.get("bozo_exception"))
            _record_feed_failure(url)
            return []
        _record_feed_success(url)
        return [_entry_to_article(e, source_name) for e in parsed.entries]
    except Exception as e:
        logger.warning("Error fetching RSS feed %s: %s", url, e)
        _record_feed_failure(url)
        return []


async def _fetch_feed_list(
    feeds: list[tuple[str, str]],
    limit: int,
) -> list[dict]:
    """Fetch a list of (name, url) feeds concurrently and return newest-first articles."""
    if not feeds:
        return []

    # Deduplicate by URL while preserving order.
    seen_urls: set[str] = set()
    unique_feeds: list[tuple[str, str]] = []
    for name, url in feeds:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        unique_feeds.append((name, url))

    custom_ua = os.getenv("NEWSDROP_USER_AGENT", "")
    default_ua = "Mozilla/5.0 (compatible; newsdrop-bot/1.0; +https://github.com/newsdrop)"
    ua = (custom_ua.strip() if isinstance(custom_ua, str) else "") or default_ua
    headers = {"User-Agent": ua}
    async with httpx.AsyncClient(headers=headers, max_redirects=5, timeout=15.0) as client:
        tasks = [_fetch_feed(client, name, url) for name, url in unique_feeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    articles: list[dict] = []
    for res in results:
        if isinstance(res, list):
            articles.extend(res)

    articles.sort(key=lambda a: a.get("publishedAt", ""), reverse=True)
    return articles[:limit]


async def fetch_rss_articles(
    country: str,
    limit: int = 30,
    category: str = "general",
) -> list[dict]:
    """Fetch articles from configured RSS feeds for a country (and optional category).

    When *category* is a non-general value with dedicated feeds, those are
    fetched first (no keyword filtering needed). Country feeds are always
    included for locality.

    Returns a list of NewsAPI-style article dicts, newest first.
    Returns [] if no feeds are configured or all fetches fail.
    """
    feeds: list[tuple[str, str]] = []
    cat = (category or "general").strip().lower()
    if cat and cat != "general":
        feeds.extend(RSS_CATEGORY_FEEDS.get(cat, []))
    feeds.extend(RSS_FEEDS.get(country, []))
    return await _fetch_feed_list(feeds, limit)


async def fetch_rss_category_articles(category: str, limit: int = 30) -> list[dict]:
    """Fetch only category-specific RSS feeds (no country general feeds)."""
    cat = (category or "").strip().lower()
    return await _fetch_feed_list(RSS_CATEGORY_FEEDS.get(cat, []), limit)


def has_rss_for(country: str, category: str = "general") -> bool:
    """Return True if any RSS feed is configured for this country/category pair."""
    cat = (category or "general").strip().lower()
    if cat and cat != "general" and RSS_CATEGORY_FEEDS.get(cat):
        return True
    return bool(RSS_FEEDS.get(country))


def has_rss_category(category: str) -> bool:
    """Return True if dedicated category RSS feeds exist."""
    cat = (category or "").strip().lower()
    return bool(RSS_CATEGORY_FEEDS.get(cat))
