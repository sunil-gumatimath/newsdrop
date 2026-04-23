# pyright: reportMissingImports=false
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from config import DAILY_REQUEST_LIMIT, ENABLE_RSS, NEWS_API_KEY
from rss_feeds import fetch_rss_articles, has_rss_for

Article = dict[str, Any]
NewsResponse = dict[str, Any]
Params = dict[str, str | int]

NEWS_LATEST_URL = "https://newsdata.io/api/1/latest"

# Simple in-memory cache: {cache_key: (timestamp, data)}
_cache: dict[str, tuple[datetime, NewsResponse]] = {}
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

_WORD_RE = re.compile(r"[a-z0-9]+")

_CATEGORY_KEYWORDS = {
    "technology": [
        "tech",
        "technology",
        "software",
        "hardware",
        "ai",
        "artificial intelligence",
        "machine learning",
        "cyber",
        "digital",
        "app",
        "application",
        "startup",
        "gadget",
        "device",
        "internet",
        "cloud",
        "data",
        "algorithm",
        "coding",
        "programming",
        "developer",
        "innovation",
        "robot",
        "automation",
        "crypto",
        "blockchain",
        "5g",
        "wireless",
        "computing",
        "chip",
        "semiconductor",
    ],
    "business": [
        "business",
        "economy",
        "market",
        "stock",
        "finance",
        "economic",
        "company",
        "corporate",
        "industry",
        "trade",
        "investment",
        "investor",
        "bank",
        "banking",
        "fund",
        "revenue",
        "profit",
        "merger",
        "acquisition",
        "ipo",
        "startup",
        "entrepreneur",
        "ceo",
        "executive",
        "commercial",
        "retail",
        "sales",
    ],
    "sports": [
        "sport",
        "game",
        "match",
        "tournament",
        "championship",
        "league",
        "team",
        "player",
        "coach",
        "athlete",
        "football",
        "soccer",
        "cricket",
        "basketball",
        "tennis",
        "hockey",
        "baseball",
        "rugby",
        "olympic",
        "race",
        "win",
        "score",
        "goal",
        "medal",
        "cup",
        "final",
        "semi-final",
        "victory",
        "defeat",
    ],
    "entertainment": [
        "movie",
        "film",
        "actor",
        "actress",
        "celebrity",
        "music",
        "song",
        "album",
        "concert",
        "artist",
        "band",
        "hollywood",
        "bollywood",
        "tv",
        "television",
        "show",
        "series",
        "netflix",
        "streaming",
        "theater",
        "cinema",
        "award",
        "oscar",
        "grammy",
        "festival",
        "entertainment",
        "celeb",
        "star",
    ],
    "health": [
        "health",
        "medical",
        "doctor",
        "hospital",
        "disease",
        "virus",
        "covid",
        "vaccine",
        "treatment",
        "medicine",
        "drug",
        "patient",
        "healthcare",
        "wellness",
        "fitness",
        "exercise",
        "diet",
        "nutrition",
        "mental health",
        "pandemic",
        "symptom",
        "cure",
        "research",
        "clinical",
        "pharmaceutical",
        "surgery",
    ],
    "science": [
        "science",
        "scientific",
        "research",
        "study",
        "scientist",
        "discovery",
        "space",
        "nasa",
        "astronomy",
        "physics",
        "chemistry",
        "biology",
        "nature",
        "climate",
        "environment",
        "earth",
        "planet",
        "universe",
        "galaxy",
        "energy",
        "experiment",
        "laboratory",
        "innovation",
        "breakthrough",
        "genetic",
        "dna",
    ],
}

logger = logging.getLogger(__name__)

# Daily API request tracking for free-tier protection
_daily_request_count = 0
_daily_request_date = date.today()
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


def _check_rate_limit() -> bool:
    """Check whether another API request may be made today."""
    global _daily_request_count, _daily_request_date

    today = date.today()
    if today != _daily_request_date:
        _daily_request_count = 0
        _daily_request_date = today

    return _daily_request_count < _request_limit


def _increment_request_count() -> None:
    global _daily_request_count
    _daily_request_count += 1


def get_request_count() -> tuple[int, int]:
    """Return current daily request usage and limit."""
    global _daily_request_count, _daily_request_date

    today = date.today()
    if today != _daily_request_date:
        _daily_request_count = 0
        _daily_request_date = today

    return _daily_request_count, _request_limit


def format_relative_time(published_at: str) -> str:
    if not published_at:
        return ""
    try:
        normalized = (
            published_at.replace("Z", "+00:00")
            if "T" in published_at or published_at.endswith("Z")
            else published_at.replace(" ", "T") + "+00:00"
        )
        pub_time = datetime.fromisoformat(normalized)
        if pub_time.tzinfo is None:
            pub_time = pub_time.replace(tzinfo=timezone.utc)

        now = datetime.now(pub_time.tzinfo)
        delta = now - pub_time
        total_seconds = int(delta.total_seconds())

        if total_seconds < 60:
            return "just now"
        if total_seconds < 3600:
            return f"{total_seconds // 60}m ago"
        if total_seconds < 86400:
            return f"{total_seconds // 3600}h ago"
        if total_seconds < 172800:
            return "yesterday"
        return f"{total_seconds // 86400}d ago"
    except Exception:
        return ""


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


def _get_from_cache(key: str) -> NewsResponse | None:
    cached = _cache.get(key)
    if cached is None:
        return None

    ts, data = cached
    if (datetime.now() - ts).total_seconds() < CACHE_TTL_SECONDS:
        return data

    del _cache[key]
    return None


def _set_cache(key: str, data: NewsResponse) -> None:
    _cache[key] = (datetime.now(), data)


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
    articles = [
        _normalize_article(article) for article in results if isinstance(article, dict)
    ]

    return {
        "status": data.get("status", "success"),
        "totalResults": data.get("totalResults", len(articles)),
        "articles": articles,
        "nextPage": data.get("nextPage"),
    }


async def _fetch_news(params: Params) -> NewsResponse:
    cache_key = _get_cache_key(params)
    cached = _get_from_cache(cache_key)
    if cached is not None:
        return cached

    if not _check_rate_limit():
        current, limit = get_request_count()
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

        if (
            response.status_code != 200
            or str(data.get("status", "")).lower() == "error"
        ):
            raise APIClientError(
                _classify_api_error(response.status_code, data),
                status_code=response.status_code,
                api_code=str(data.get("code", "")) or None,
            )

        normalized = _normalize_response(data)
        _set_cache(cache_key, normalized)
        _increment_request_count()
        return normalized


def _normalize_title(title: str) -> str:
    if not title:
        return ""
    return " ".join(_WORD_RE.findall(title.lower()))


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
    if category == "general" or category not in _CATEGORY_KEYWORDS:
        return articles

    keywords = _CATEGORY_KEYWORDS[category]
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

    api_task = _fetch_news(params)
    rss_task = _safe_fetch_rss(country, limit=20)

    api_data: NewsResponse | None = None
    api_error: Exception | None = None

    try:
        api_data, rss_articles = await asyncio.gather(api_task, rss_task)
    except Exception:
        try:
            api_data = await api_task
        except Exception as exc:
            api_error = exc
            logger.warning("NewsData.io fetch failed, will try RSS: %s", exc)
        rss_articles = await rss_task
    else:
        api_error = None

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


def generate_summary(article: Article) -> str:
    title = str(article.get("title", "No title"))
    description = article.get("description", "")
    content = article.get("content", "")

    summary = description or content
    if not summary:
        return _escape_text(title)

    sentences = str(summary).replace("..", ".").split(". ")
    short = ". ".join(sentences[:3]).strip()
    short = _truncate_text(short, 200)
    return _escape_text(short)


def format_briefing(
    data: NewsResponse,
    country: str = "us",
    category: str = "general",
) -> str:
    raw_articles = data.get("articles", [])
    articles: list[Article] = raw_articles if isinstance(raw_articles, list) else []

    if not articles:
        return (
            f"No {_escape_text(category)} news articles found for "
            f"{_escape_text(country.upper())}. Try again later."
        )

    first_article = articles[0] if articles else {}
    published_at = str(first_article.get("publishedAt", ""))
    date_str = (
        published_at[:10] if published_at else datetime.now().strftime("%Y-%m-%d")
    )

    cat_label = category.capitalize() if category != "general" else "Top"
    message = (
        f"📰 <b>Daily News Briefing — {_escape_text(date_str)}</b>\n"
        f"🌍 {_escape_text(cat_label)} Headlines ({_escape_text(country.upper())})\n\n"
    )

    for i, article in enumerate(articles[:10], 1):
        source_obj = article.get("source", {})
        source = (
            _escape_text(source_obj.get("name", "Unknown"))
            if isinstance(source_obj, dict)
            else "Unknown"
        )
        title = str(article.get("title", "No title"))
        url = str(article.get("url", ""))
        description = article.get("description", "")
        pub_time = str(article.get("publishedAt", ""))

        time_label = (
            f" · {_escape_text(format_relative_time(pub_time))}" if pub_time else ""
        )
        message += f"{_format_linked_title(i, title, url)}{time_label}\n"

        if description:
            desc = _truncate_text(str(description), 150)
            message += f"<i>{_escape_text(desc)}</i>\n"

        message += f"📍 {source}\n\n"

    message += "Stay informed! 🌍"
    return message


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
        summary = generate_summary(article)

        message += f"{_format_linked_title(i, title, url)}\n"
        message += f"<i>{summary}</i>\n"
        message += f"📍 {source}\n\n"

    total_results = data.get("totalResults", 0)
    message += f"Found {total_results} results. Showing top 10."
    return message


def get_article_image(article: Article) -> str | None:
    url = _safe_url(article.get("urlToImage", ""))
    if url and not url.endswith("null"):
        return url
    return None


async def fetch_breaking_news(
    countries: list[str], keywords: list[str]
) -> list[Article]:
    breaking_articles: list[Article] = []

    for country in countries:
        params: Params = {
            "apikey": NEWS_API_KEY or "",
            "country": country,
            "language": "en",
            "size": 10,
        }

        cache_key = _get_cache_key(params)
        cached = _get_from_cache(cache_key)
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

            if any(keyword.lower() in combined for keyword in keywords):
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


async def fetch_trending_topics(countries: list[str]) -> dict[str, int]:
    keyword_counts: dict[str, int] = {}

    for country in countries:
        params: Params = {
            "apikey": NEWS_API_KEY or "",
            "country": country,
            "language": "en",
            "size": 10,
        }

        cache_key = _get_cache_key(params)
        cached = _get_from_cache(cache_key)
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
