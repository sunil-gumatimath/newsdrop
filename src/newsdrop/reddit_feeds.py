"""Reddit subreddit fetcher for cross-source news corroboration.

Uses Reddit's public JSON endpoints (no API key required). Posts are
normalised to the same NewsAPI-style article dict used by RSS and
NewsData.io so they can be matched against headlines in cross_verify.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import feedparser
import httpx

logger = logging.getLogger(__name__)

Article = dict[str, Any]

# Curated subreddits keyed by ISO 3166-1 alpha-2 country code.
REDDIT_SUBREDDITS: dict[str, list[str]] = {
    "world": ["worldnews", "news", "geopolitics"],
    "us": ["news", "worldnews", "usnews"],
    "gb": ["unitedkingdom", "uknews"],
    "in": ["india", "IndiaSpeaks"],
    "ca": ["canada", "CanadaNews"],
    "au": ["australia", "AusNews"],
    "de": ["de", "germany"],
    "fr": ["france", "europe"],
    "jp": ["japan", "japannews"],
    "br": ["brazil", "worldnews"],
    "kr": ["korea", "worldnews"],
}

# Category-specific subreddits merged with country lists when fetching.
REDDIT_CATEGORY_SUBREDDITS: dict[str, list[str]] = {
    "general": ["news", "worldnews"],
    "technology": ["technology", "gadgets", "Futurology"],
    "business": ["business", "economics", "stocks"],
    "sports": ["sports"],
    "entertainment": ["movies", "television", "Music"],
    "health": ["health", "medicine"],
    "science": ["science", "space"],
}

REDDIT_BASE_URL = "https://www.reddit.com"
HTTP_TIMEOUT_SECONDS = 15.0


def _reddit_user_agent() -> str:
    custom_ua = os.getenv("NEWSDROP_USER_AGENT")
    return custom_ua or "Mozilla/5.0 (compatible; newsdrop-bot/1.0; +https://github.com/newsdrop)"


def _is_external_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.netloc or "").lower()
    return host not in {"reddit.com", "www.reddit.com", "old.reddit.com", "redd.it"}


def _post_to_article(post: dict[str, Any], subreddit: str) -> Article:
    title = str(post.get("title", "") or "No title")
    raw_url = str(post.get("url", "") or "")
    external_url = raw_url if _is_external_url(raw_url) else ""
    permalink = str(post.get("permalink", "") or "")
    reddit_url = f"{REDDIT_BASE_URL}{permalink}" if permalink else ""
    created = post.get("created_utc")
    published_at = ""
    if isinstance(created, int | float):
        published_at = str(datetime.fromtimestamp(created, tz=UTC).isoformat())

    return {
        "title": title,
        "url": external_url,
        "description": "",
        "content": "",
        "urlToImage": "",
        "publishedAt": published_at,
        "source": {"name": f"r/{subreddit}"},
        "redditSubreddit": subreddit,
        "redditScore": int(post.get("score", 0) or 0),
        "redditPermalink": reddit_url,
        "redditIsSelf": bool(post.get("is_self", False)),
    }


def _parse_reddit_feed_date(entry: dict[str, Any]) -> str:
    raw = str(entry.get("published") or entry.get("updated") or "")
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC).isoformat()
    except ValueError:
        return ""


def _feed_entry_to_article(entry: dict[str, Any], subreddit: str) -> Article:
    title = str(entry.get("title", "") or "No title")
    reddit_url = str(entry.get("link", "") or "")
    return {
        "title": title,
        "url": "",
        "description": "",
        "content": "",
        "urlToImage": "",
        "publishedAt": _parse_reddit_feed_date(entry),
        "source": {"name": f"r/{subreddit}"},
        "redditSubreddit": subreddit,
        "redditScore": 0,
        "redditPermalink": reddit_url,
        "redditIsSelf": False,
    }


async def _fetch_subreddit_rss(
    client: httpx.AsyncClient,
    subreddit: str,
    limit: int = 25,
) -> list[Article]:
    url = f"{REDDIT_BASE_URL}/r/{subreddit}/.rss"
    try:
        response = await client.get(url)
        if response.status_code != 200:
            logger.warning("Reddit RSS r/%s returned HTTP %s", subreddit, response.status_code)
            return []
        if len(response.content) >= 2_000_000:
            logger.warning("Reddit RSS r/%s response too large (>=2MB), skipping", subreddit)
            return []
        parsed = await asyncio.to_thread(feedparser.parse, response.content)
    except Exception as exc:
        logger.warning("Error fetching Reddit RSS r/%s: %s", subreddit, exc)
        return []

    entries = parsed.get("entries", [])
    if not isinstance(entries, list):
        return []
    return [
        _feed_entry_to_article(entry, subreddit)
        for entry in entries[:limit]
        if isinstance(entry, dict)
    ]


async def _fetch_subreddit(
    client: httpx.AsyncClient,
    subreddit: str,
    limit: int = 25,
) -> list[Article]:
    url = f"{REDDIT_BASE_URL}/r/{subreddit}/hot.json"
    params = {"limit": str(limit), "raw_json": "1"}
    try:
        response = await client.get(url, params=params)
        if response.status_code != 200:
            logger.warning(
                "Reddit r/%s returned HTTP %s; trying RSS fallback", subreddit, response.status_code
            )
            return await _fetch_subreddit_rss(client, subreddit, limit=limit)
        if len(response.content) >= 2_000_000:
            logger.warning("Reddit r/%s response too large (>=2MB), skipping", subreddit)
            return []
        payload = response.json()
    except Exception as exc:
        logger.warning("Error fetching Reddit r/%s: %s", subreddit, exc)
        return []

    children = payload.get("data", {}).get("children", [])
    if not isinstance(children, list):
        return []

    articles: list[Article] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        post = child.get("data")
        if not isinstance(post, dict):
            continue
        if post.get("stickied"):
            continue
        articles.append(_post_to_article(post, subreddit))
    return articles


def _subreddits_for(country: str, category: str) -> list[str]:
    names: list[str] = []
    names.extend(REDDIT_SUBREDDITS.get(country, []))
    names.extend(REDDIT_CATEGORY_SUBREDDITS.get(category, []))
    if not names:
        names.extend(REDDIT_CATEGORY_SUBREDDITS.get("general", []))

    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(name)
    return ordered


async def fetch_reddit_posts(
    country: str,
    category: str = "general",
    limit_per_sub: int = 25,
) -> list[Article]:
    """Fetch hot posts from curated subreddits for a country/category pair."""
    subreddits = _subreddits_for(country, category)
    if not subreddits:
        return []

    headers = {"User-Agent": _reddit_user_agent()}
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, max_redirects=5) as client:
        tasks = [_fetch_subreddit(client, sub, limit_per_sub) for sub in subreddits]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    articles: list[Article] = []
    for res in results:
        if isinstance(res, list):
            articles.extend(res)

    articles.sort(key=lambda a: str(a.get("publishedAt", "") or ""), reverse=True)
    return articles


def has_reddit_for(country: str, category: str = "general") -> bool:
    """Return True if at least one subreddit is configured for this combo."""
    return bool(_subreddits_for(country, category))
