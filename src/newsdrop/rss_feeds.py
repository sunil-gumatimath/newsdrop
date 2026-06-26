"""RSS feed source for multi-source news aggregation.

Provides a curated catalog of RSS feeds per country and an async fetcher
that returns articles in the same NewsAPI-style shape used elsewhere in
the project (so they can be merged seamlessly with NewsData.io results).
"""

import asyncio
import html as html_lib
import logging
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import feedparser
import httpx

logger = logging.getLogger(__name__)

# Curated RSS feed catalog.  Keys are ISO 3166-1 alpha-2 country codes to
# match the NewsData.io `country` param used elsewhere.  Each feed is:
#   (source_display_name, url)
RSS_FEEDS: dict[str, list[tuple[str, str]]] = {
    "in": [
        ("Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"),
        ("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss"),
        ("NDTV", "https://feeds.feedburner.com/ndtvnews-top-stories"),
        ("Indian Express", "https://indianexpress.com/section/india/feed/"),
        ("Hindustan Times", "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml"),
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
    "ca": [
        ("CBC", "https://www.cbc.ca/webfeed/rss/rss-topstories"),
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
    "br": [
        ("G1 Globo", "https://g1.globo.com/rss/g1/"),
    ],
    "kr": [
        ("Chosun English", "https://english.chosun.com/site/data/rss/rss.xml"),
    ],
}


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string (RSS summaries often contain markup)."""
    if not text:
        return ""
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
                dt = datetime(*t[:6], tzinfo=UTC)
                return dt.isoformat()
            except Exception:
                pass

    # Fallback: raw string
    raw = entry.get("published") or entry.get("updated") or ""
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.isoformat()
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
            return url
    # <media:thumbnail url="..."/>
    thumbs = entry.get("media_thumbnail") or []
    if thumbs and isinstance(thumbs, list):
        url = thumbs[0].get("url")
        if url:
            return url
    # <enclosure type="image/...">
    for enc in entry.get("enclosures", []) or []:
        if enc.get("type", "").startswith("image/") and enc.get("href"):
            return enc["href"]
    # image in summary/content
    summary = entry.get("summary") or ""
    match = re.search(r'<img[^>]+src="([^"]+)"', summary)
    if match:
        return match.group(1)
    return ""


def _entry_to_article(entry: dict, source_name: str) -> dict:
    """Convert a feedparser entry into the NewsAPI-style shape used by bot.py."""
    title = _strip_html(entry.get("title", "")) or "No title"
    url = entry.get("link", "") or ""
    description = _strip_html(entry.get("summary", "") or entry.get("description", ""))
    # Truncate overly long RSS summaries
    if len(description) > 500:
        description = description[:497] + "..."

    return {
        "title": title,
        "url": url,
        "description": description,
        "content": description,
        "urlToImage": _extract_image(entry),
        "publishedAt": _parse_rss_date(entry),
        "source": {"name": source_name},
    }


async def _fetch_feed(client: httpx.AsyncClient, source_name: str, url: str) -> list[dict]:
    """Fetch and parse a single RSS feed. Returns [] on any failure."""
    try:
        response = await client.get(url, timeout=8.0, follow_redirects=True)
        if response.status_code != 200:
            logger.warning("RSS feed %s returned HTTP %s", url, response.status_code)
            return []
        # feedparser is synchronous but parses bytes quickly; run inline.
        parsed = feedparser.parse(response.content)
        if parsed.bozo and not parsed.entries:
            logger.warning(
                "RSS feed %s failed to parse: %s", url, parsed.get("bozo_exception")
            )
            return []
        return [_entry_to_article(e, source_name) for e in parsed.entries]
    except Exception as e:
        logger.warning("Error fetching RSS feed %s: %s", url, e)
        return []


async def fetch_rss_articles(country: str, limit: int = 30) -> list[dict]:
    """Fetch articles from all configured RSS feeds for a country.

    Returns a list of NewsAPI-style article dicts, newest first.
    Returns [] if no feeds are configured for the country or all fetches fail.
    """
    feeds = RSS_FEEDS.get(country, [])
    if not feeds:
        return []

    # Use a shared httpx client with a reasonable UA to avoid some 403s.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; newsdrop-bot/1.0; "
            "+https://github.com/newsdrop)"
        )
    }
    async with httpx.AsyncClient(headers=headers) as client:
        tasks = [_fetch_feed(client, name, url) for name, url in feeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    articles: list[dict] = []
    for res in results:
        if isinstance(res, list):
            articles.extend(res)

    # Sort by publishedAt desc (empty dates sink to the bottom)
    articles.sort(key=lambda a: a.get("publishedAt", ""), reverse=True)

    return articles[:limit]


def has_rss_for(country: str) -> bool:
    """Return True if we have at least one configured RSS feed for this country."""
    return bool(RSS_FEEDS.get(country))
