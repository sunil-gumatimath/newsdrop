"""newsdrop news fetcher.

Shared state (API response cache and daily request budget) lives in
``newsdrop.state`` so the bot can run multiple workers when Redis is
configured. With no ``REDIS_URL`` set, the state module falls back to an
in-memory backend for local / single-worker deployments.
"""

# pyright: reportMissingImports=false
from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import (
    CATEGORY_KEYWORDS,
    DAILY_REQUEST_LIMIT,
    ENABLE_RSS,
    NEWS_API_KEY,
    WORD_RE,
)
from .rss_feeds import fetch_rss_articles, has_rss_for
from .state import (
    api_budget_check,
    api_request_consume,
    api_request_count,
    cache_get,
    cache_set,
)

Article = dict[str, Any]
NewsResponse = dict[str, Any]
Params = dict[str, str | int]

NEWS_LATEST_URL = "https://newsdata.io/api/1/latest"

CACHE_TTL_SECONDS = 300  # 5 minutes
HTTP_TIMEOUT_SECONDS = 15.0

CATEGORY_MAP = {
    "general": "top",
    "technology": "technology",
    "business": "business",
    "sports": "sports",
    "entertainment": "entertainment",
    "health": "health",
    "science": "science",
}

# _WORD_RE and _CATEGORY_KEYWORDS were moved to config.py as WORD_RE and
# CATEGORY_KEYWORDS so all tunable taxonomy lists live in one place. They are
# imported at the top of this module from `config`.

logger = logging.getLogger(__name__)

# Daily API request tracking for free-tier protection.
_request_limit = DAILY_REQUEST_LIMIT if DAILY_REQUEST_LIMIT > 0 else 200


class APIClientError(Exception):
    """Custom exception for NewsData.io errors with structured info."""

    status_code: int | None
    api_code: str | None

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        api_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code


async def get_request_count() -> tuple[int, int]:
    """Return current daily request usage and limit."""
    return await api_request_count(_request_limit)


def _escape_text(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _safe_url(url: object) -> str:
    if not isinstance(url, str):
        return ""

    candidate = url.strip()
    if not candidate:
        return ""

    try:
        parsed = urlparse(candidate)
    except Exception:
        return ""

    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.netloc:
        return ""

    return candidate


def _format_linked_title(index: int, title: str, url: str) -> str:
    safe_title = _escape_text(title or "No title")
    safe_url = _safe_url(url)

    if safe_url:
        escaped_url = html.escape(safe_url, quote=True)
        return f'<b>{index}. <a href="{escaped_url}">{safe_title}</a></b>'
    return f"<b>{index}. {safe_title}</b>"


def _truncate_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def _get_cache_key(params: Params) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "apikey")


def _classify_api_error(status_code: int, body: NewsResponse) -> str:
    status = str(body.get("status", "")).lower()
    results = body.get("results")
    result_code = str(body.get("code", ""))
    message = str(body.get("message", "") or body.get("results", ""))

    if status_code in {401, 403}:
        return "⚠️ The news API key is invalid. Please check your configuration."
    if status_code == 429:
        return "⏳ The news API rate limit has been reached. Please try again in a few minutes."
    if status_code >= 500:
        return "🔧 The news service is experiencing issues. Please try again later."

    if status == "error":
        lower_message = message.lower()
        if "api key" in lower_message or "unauthorized" in lower_message:
            return "⚠️ The news API key is invalid. Please check your configuration."
        if "rate limit" in lower_message or "quota" in lower_message:
            return "⏳ The news API rate limit has been reached. Please try again in a few minutes."
        if message:
            return f"⚠️ News API error: {_escape_text(message)}"

    if result_code:
        return f"⚠️ News API error: {_escape_text(result_code)}"
    if isinstance(results, str) and results:
        return f"⚠️ News API error: {_escape_text(results)}"
    if message:
        return f"⚠️ News API error: {_escape_text(message)}"

    return "⚠️ An unexpected error occurred while fetching news. Please try again later."


def _normalize_article(article: Article) -> Article:
    source_name = article.get("source_name") or article.get("source_id") or "Unknown"
    category = article.get("category", [])
    country = article.get("country", [])

    return {
        "source": {"name": source_name},
        "title": article.get("title") or "No title",
        "description": article.get("description") or "",
        "content": article.get("content") or "",
        "url": article.get("link") or "",
        "urlToImage": article.get("image_url") or "",
        "publishedAt": article.get("pubDate") or "",
        "category": category if isinstance(category, list) else [],
        "country": country if isinstance(country, list) else [],
        "creator": article.get("creator") or [],
    }


def _normalize_response(data: NewsResponse) -> NewsResponse:
    raw_results = data.get("results", [])
    results: list[Article] = raw_results if isinstance(raw_results, list) else []
    articles = [_normalize_article(article) for article in results if isinstance(article, dict)]

    return {
        "status": data.get("status", "success"),
        "totalResults": data.get("totalResults", len(articles)),
        "articles": articles,
        "nextPage": data.get("nextPage"),
    }


async def _fetch_news(params: Params) -> NewsResponse:
    cache_key = _get_cache_key(params)
    cached = await cache_get(cache_key)
    if isinstance(cached, dict):
        return cached

    if not await api_budget_check(_request_limit):
        current, limit = await get_request_count()
        raise APIClientError(
            f"Daily API request limit reached ({current}/{limit}). "
            "Please try again tomorrow or disable RSS fallback if needed.",
            status_code=429,
            api_code="RateLimitExceeded",
        )

    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(NEWS_LATEST_URL, params=params)
        except httpx.TimeoutException as exc:
            raise APIClientError(
                "⏳ The news service took too long to respond. Please try again."
            ) from exc
        except httpx.HTTPError as exc:
            raise APIClientError(
                "🔌 Unable to reach the news service right now. Please try again later."
            ) from exc

        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"News service returned invalid response (HTTP {response.status_code})"
            ) from exc

        if not isinstance(data, dict):
            raise RuntimeError(
                f"News service returned unexpected response shape (HTTP {response.status_code})"
            )

        if response.status_code != 200 or str(data.get("status", "")).lower() == "error":
            raise APIClientError(
                _classify_api_error(response.status_code, data),
                status_code=response.status_code,
                api_code=str(data.get("code", "")) or None,
            )

        normalized = _normalize_response(data)
        await cache_set(cache_key, normalized, CACHE_TTL_SECONDS)
        await api_request_consume(_request_limit)
        return normalized


def _normalize_title(title: str) -> str:
    if not title:
        return ""
    return " ".join(WORD_RE.findall(title.lower()))


def _merge_and_dedupe(*sources: list[Article], limit: int = 10) -> list[Article]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    merged: list[Article] = []

    for source in sources:
        for article in source:
            url = str(article.get("url", "")).strip()
            title_key = _normalize_title(str(article.get("title", "")))

            if url and url in seen_urls:
                continue
            if title_key and title_key in seen_titles:
                continue

            if url:
                seen_urls.add(url)
            if title_key:
                seen_titles.add(title_key)

            merged.append(article)

    merged.sort(key=lambda a: str(a.get("publishedAt", "") or ""), reverse=True)
    return merged[:limit]


async def _safe_fetch_rss(country: str, limit: int = 20) -> list[Article]:
    if not ENABLE_RSS or not has_rss_for(country):
        return []
    try:
        return await fetch_rss_articles(country, limit=limit)
    except Exception as exc:
        logger.warning("RSS fetch failed for %s: %s", country, exc)
        return []


def _filter_by_query(articles: list[Article], query: str) -> list[Article]:
    q = query.lower().strip()
    if not q:
        return articles

    result: list[Article] = []
    for article in articles:
        blob = f"{article.get('title', '')} {article.get('description', '')}"
        if q in blob.lower():
            result.append(article)
    return result


def _filter_by_category(articles: list[Article], category: str) -> list[Article]:
    if category == "general" or category not in CATEGORY_KEYWORDS:
        return articles

    keywords = CATEGORY_KEYWORDS[category]
    min_matches = 1 if category == "sports" else 2

    filtered: list[Article] = []
    for article in articles:
        title = str(article.get("title", "")).lower()
        description = str(article.get("description", "")).lower()
        blob = f"{title} {description}"
        match_count = sum(1 for keyword in keywords if keyword in blob)

        if match_count >= min_matches:
            filtered.append(article)

    return filtered


async def fetch_top_headlines(
    country: str = "us",
    category: str = "general",
) -> NewsResponse:
    mapped_category = CATEGORY_MAP.get(category, category)
    params: Params = {
        "apikey": NEWS_API_KEY or "",
        "country": country,
        "language": "en",
        "size": 10,
    }

    if mapped_category and mapped_category != "top":
        params["category"] = mapped_category

    api_result, rss_result = await asyncio.gather(
        _fetch_news(params),
        _safe_fetch_rss(country, limit=20),
        return_exceptions=True,
    )

    api_data: NewsResponse | None = None
    api_error: Exception | None = None
    rss_articles: list[Article] = []

    if isinstance(api_result, Exception):
        api_error = api_result
        logger.warning("NewsData.io fetch failed, will try RSS: %s", api_result)
    elif isinstance(api_result, dict):
        api_data = api_result

    if isinstance(rss_result, Exception):
        logger.warning("RSS fetch failed for %s: %s", country, rss_result)
    else:
        rss_articles = rss_result

    rss_articles = _filter_by_category(rss_articles, category)
    api_articles = (api_data or {}).get("articles", [])
    api_articles = api_articles if isinstance(api_articles, list) else []

    if not api_articles and not rss_articles and api_error is not None:
        raise api_error

    merged = _merge_and_dedupe(api_articles, rss_articles, limit=10)

    sources_used: list[str] = []
    if api_articles:
        sources_used.append("newsdata.io")
    if rss_articles:
        sources_used.append("rss")

    return {
        "status": "ok",
        "totalResults": len(merged),
        "articles": merged,
        "sources": sources_used,
    }


async def search_news(query: str, country: str = "us") -> NewsResponse:
    params: Params = {
        "apikey": NEWS_API_KEY or "",
        "q": query,
        "language": "en",
        "size": 10,
    }

    api_data: NewsResponse | None = None
    api_error: Exception | None = None

    try:
        api_data = await _fetch_news(params)
    except Exception as exc:
        api_error = exc
        logger.warning("NewsData.io search failed, will try RSS: %s", exc)

    rss_articles = await _safe_fetch_rss(country, limit=50)
    rss_articles = _filter_by_query(rss_articles, query)

    api_articles = (api_data or {}).get("articles", [])
    api_articles = api_articles if isinstance(api_articles, list) else []

    if not api_articles and not rss_articles and api_error is not None:
        raise api_error

    merged = _merge_and_dedupe(api_articles, rss_articles, limit=10)

    return {
        "status": "ok",
        "totalResults": len(merged),
        "articles": merged,
    }


def format_search_results(data: NewsResponse, query: str) -> str:
    raw_articles = data.get("articles", [])
    articles: list[Article] = raw_articles if isinstance(raw_articles, list) else []

    if not articles:
        return f'No results found for "{_escape_text(query)}". Try a different search term.'

    message = f'🔍 <b>Search Results: "{_escape_text(query)}"</b>\n\n'

    for i, article in enumerate(articles[:10], 1):
        source_obj = article.get("source", {})
        source = (
            _escape_text(source_obj.get("name", "Unknown"))
            if isinstance(source_obj, dict)
            else "Unknown"
        )
        title = str(article.get("title", "No title"))
        url = str(article.get("url", ""))
        summary = _truncate_text(
            str(article.get("description", "") or article.get("content", "") or ""), 200
        )

        message += f"{_format_linked_title(i, title, url)}\n"
        message += f"<i>{_escape_text(summary)}</i>\n"
        message += f"📍 {source}\n\n"

    total_results = data.get("totalResults", 0)
    message += f"Found {total_results} results. Showing top 10."
    return message


def get_article_image(article: Article) -> str | None:
    url = _safe_url(article.get("urlToImage", ""))
    if url and not url.endswith("null"):
        return url
    return None


async def fetch_breaking_news(countries: list[str], keywords: list[str]) -> list[Article]:
    # P1 #3 — guard against API budget exhaustion.
    # BREAKING_ALERT_INTERVAL_MINUTES (default 30) means the cron fires 48x/day.
    # Without this gate, N countries × 48 cycles can blow past DAILY_REQUEST_LIMIT.
    # We stop iterating countries the moment the daily budget is gone, so later
    # countries share the same budget exhaustion rather than each one issuing
    # a doomed request. The cache is per-params-key, so a cache hit short-
    # circuits _fetch_news entirely (no budget cost) — this gate only matters
    # on cache misses, which is exactly the over-budget case we're fixing.
    breaking_articles: list[Article] = []

    for country in countries:
        # Rate-limit gate: if the daily budget is gone, log once and bail out
        # of the whole country loop (do not "continue" — remaining countries
        # would just hit the same gate and spam the log).
        if not await api_budget_check(_request_limit):
            current, limit = await get_request_count()
            logger.warning(
                "Breaking-news fetch skipped for %s: daily budget exhausted "
                "(%s/%s). Remaining countries will be skipped this cycle.",
                country,
                current,
                limit,
            )
            break

        params: Params = {
            "apikey": NEWS_API_KEY or "",
            "country": country,
            "language": "en",
            "size": 10,
        }

        cache_key = _get_cache_key(params)
        cached = await cache_get(cache_key)
        if cached:
            raw_articles = cached.get("articles", [])
            articles = raw_articles if isinstance(raw_articles, list) else []
        else:
            try:
                data = await _fetch_news(params)
                raw_articles = data.get("articles", [])
                articles = raw_articles if isinstance(raw_articles, list) else []
            except Exception:
                continue

        for article in articles:
            title = str(article.get("title", "")).lower()
            description = str(article.get("description", "")).lower()
            combined = f"{title} {description}"

            # P1 #4 — word-boundary keyword match.
            # Previous substring check produced noisy false positives:
            #   "war" matched "Star Wars", "attack" matched "attack ad",
            #   "fire" matched "laid off", "crash" matched "app crash".
            # Word boundaries fix all of those. Trade-off: "Star Wars" still
            # matches "war" because "Wars" is a distinct token — acceptable,
            # those headlines are rare in /news and one FP/day beats dozens
            # of "attack ad" hits.
            matched = [
                kw for kw in keywords if re.search(rf"\b{re.escape(kw.lower())}\b", combined)
            ]
            # Require the keyword to land in the title, OR have at least two
            # distinct keywords hit anywhere in title+description (a strong
            # signal that the article is genuinely about a tracked topic).
            title_hits = [kw for kw in matched if re.search(rf"\b{re.escape(kw.lower())}\b", title)]
            if title_hits or len(matched) >= 2:
                tagged = dict(article)
                tagged["country"] = country
                breaking_articles.append(tagged)

    return breaking_articles


def extract_keywords(text: str) -> list[str]:
    common_words = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "as",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "they",
        "them",
        "their",
        "he",
        "him",
        "his",
        "she",
        "her",
        "hers",
        "we",
        "us",
        "our",
        "you",
        "your",
        "i",
        "me",
        "my",
        "what",
        "which",
        "who",
        "whom",
        "when",
        "where",
        "why",
        "how",
        "if",
        "then",
        "else",
        "while",
        "after",
        "before",
        "between",
        "into",
        "through",
        "during",
        "until",
        "against",
        "without",
        "within",
        "upon",
        "about",
        "above",
        "below",
        "over",
        "under",
        "again",
        "further",
        "once",
        "here",
        "there",
        "all",
        "any",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "now",
        "says",
        "said",
        "new",
        "news",
        "report",
        "reports",
        "update",
        "updates",
    }

    words = text.lower().replace("-", " ").replace("'", " ").split()
    return [word for word in words if len(word) > 2 and word not in common_words]


async def fetch_trending_topics(countries: list[str], category: str = "general") -> dict[str, int]:
    keyword_counts: dict[str, int] = {}
    mapped_category = CATEGORY_MAP.get(category, category)

    for country in countries:
        params: Params = {
            "apikey": NEWS_API_KEY or "",
            "country": country,
            "language": "en",
            "size": 10,
        }

        if mapped_category and mapped_category != "top":
            params["category"] = mapped_category

        cache_key = _get_cache_key(params)
        cached = await cache_get(cache_key)
        if cached:
            raw_articles = cached.get("articles", [])
            articles = raw_articles if isinstance(raw_articles, list) else []
        else:
            try:
                data = await _fetch_news(params)
                raw_articles = data.get("articles", [])
                articles = raw_articles if isinstance(raw_articles, list) else []
            except Exception:
                continue

        rss_articles = await _safe_fetch_rss(country, limit=20)
        rss_articles = _filter_by_category(rss_articles, category)
        articles = _merge_and_dedupe(articles, rss_articles, limit=20)

        for article in articles:
            title = str(article.get("title", "") or "")
            for keyword in extract_keywords(title):
                keyword_counts[keyword] = keyword_counts.get(keyword, 0) + 1

    sorted_topics = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)
    return dict(sorted_topics[:10])


async def check_api_health() -> dict[str, str]:
    params: Params = {
        "apikey": NEWS_API_KEY or "",
        "country": "us",
        "language": "en",
        "size": 1,
    }

    try:
        timeout = httpx.Timeout(10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(NEWS_LATEST_URL, params=params)

            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict) and data.get("status") == "error":
                    results = data.get("results")
                    if isinstance(results, dict):
                        err = results.get("message", "Unknown error")
                    else:
                        err = data.get("message", "Unknown error")
                    return {
                        "status": "unhealthy",
                        "error": f"API error: {err}",
                    }

                return {
                    "status": "healthy",
                    "response_time": f"{response.elapsed.total_seconds():.2f}s",
                }

            if response.status_code == 401:
                return {"status": "unhealthy", "error": "Invalid API key"}
            if response.status_code == 429:
                return {"status": "unhealthy", "error": "Rate limit exceeded"}

            return {
                "status": "unhealthy",
                "error": f"HTTP {response.status_code}",
            }
    except Exception as exc:
        return {
            "status": "unhealthy",
            "error": str(exc),
        }
