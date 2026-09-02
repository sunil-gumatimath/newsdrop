"""Hacker News Algolia feed — free, no API key.

Endpoint: https://hn.algolia.com/api/v1/search_by_date?tags=story
Docs: https://hn.algolia.com/api

Mapped to newsdrop Article shape for rank_and_cluster.
Only fetched for tech/science categories or when query looks techy, to avoid
diluting general briefings.  No budget gate — HN is unlimited free.

Uses the shared httpx client from news_fetcher for connection reuse.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any
from urllib.parse import urlparse

from .config import ENABLE_HN

logger = logging.getLogger(__name__)

Article = dict[str, Any]

HN_API_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_TIMEOUT_SECONDS = 10.0

# Only pull HN for these categories (tech/science benefit most).
HN_RELEVANT_CATEGORIES = {"technology", "science", "general"}


def _safe_url(url: object) -> str:
    if not isinstance(url, str):
        return ""
    cand = url.strip()
    if not cand:
        return ""
    try:
        parsed = urlparse(cand)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return cand


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _hn_hit_to_article(hit: dict[str, Any]) -> Article | None:
    if not isinstance(hit, dict):
        return None
    title = _clean_text(hit.get("title") or hit.get("story_title") or "")
    if not title:
        return None
    object_id = str(hit.get("objectID") or "")
    # Prefer story URL, fallback to HN discussion
    raw_url = hit.get("url") or hit.get("story_url") or ""
    url = _safe_url(raw_url)
    if not url and object_id:
        url = f"https://news.ycombinator.com/item?id={object_id}"
    url = _safe_url(url)
    if not url:
        return None
    # Description: story_text or first line of comment text
    desc = _clean_text(hit.get("story_text") or hit.get("comment_text") or "")
    # Add points/comments context if no description
    if not desc:
        points = hit.get("points")
        comments = hit.get("num_comments")
        bits = []
        if isinstance(points, int) and points > 0:
            bits.append(f"{points} points")
        if isinstance(comments, int) and comments > 0:
            bits.append(f"{comments} comments")
        if bits:
            desc = " · ".join(bits)
    created_at = str(hit.get("created_at") or hit.get("created_at_i") or "")
    # created_at is ISO8601 already
    return {
        "source": {"name": "Hacker News"},
        "title": title,
        "description": desc[:500] if desc else "",
        "content": desc,
        "url": url,
        "urlToImage": "",
        "publishedAt": created_at,
        "category": ["technology"],
        "country": ["us"],
        "creator": [str(hit.get("author") or "HN")],
        "_hn_objectID": object_id,
        "_hn_points": hit.get("points"),
    }


async def fetch_hn_articles(
    limit: int = 10,
    query: str | None = None,
    category: str = "technology",
) -> list[Article]:
    """Fetch Hacker News articles via Algolia.

    - limit: max articles (capped to 20)
    - query: optional search term (whole-word filtered later)
    - category: used to skip non-tech categories unless query is techy
    """
    if not ENABLE_HN:
        return []
    # Skip HN for non-tech categories unless query explicitly techy
    cat = (category or "general").strip().lower()
    if cat not in HN_RELEVANT_CATEGORIES and not query:
        return []

    limit = max(1, min(20, int(limit)))
    params: dict[str, Any] = {
        "tags": "story",
        "hitsPerPage": limit,
        "page": 0,
    }
    if query and query.strip():
        params["query"] = query.strip()[:100]

    # Lazy import to avoid circular deps (news_fetcher imports this module)
    from .news_fetcher import get_http_client

    client = await get_http_client()
    try:
        resp = await client.get(HN_API_URL, params=params, timeout=HN_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("HN fetch failed (query=%r cat=%s): %s", query, category, exc)
        return []

    hits = data.get("hits", []) if isinstance(data, dict) else []
    if not isinstance(hits, list):
        return []
    articles: list[Article] = []
    for hit in hits:
        art = _hn_hit_to_article(hit)
        if art:
            articles.append(art)
            if len(articles) >= limit:
                break
    return articles
