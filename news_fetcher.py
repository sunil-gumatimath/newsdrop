# pyright: reportMissingImports=false
from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from config import NEWS_API_KEY

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


def format_relative_time(published_at: str) -> str:
    if not published_at:
        return ""
    try:
        normalized = (
            published_at.replace("Z", "+00:00")
            if "T" in published_at
            else published_at.replace(" ", "T") + "+00:00"
        )
        pub_time = datetime.fromisoformat(normalized)
        now = datetime.now(pub_time.tzinfo)
        delta = now - pub_time
        total_seconds = int(delta.total_seconds())

        if total_seconds < 60:
            return "just now"
        if total_seconds < 3600:
            mins = total_seconds // 60
            return f"{mins}m ago"
        if total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours}h ago"
        if total_seconds < 172800:
            return "yesterday"
        days = total_seconds // 86400
        return f"{days}d ago"
    except Exception:
        return ""


def _escape_text(value: object) -> str:
    """Escape text for Telegram HTML parse mode."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _safe_url(url: object) -> str:
    """Return a Telegram-safe http/https URL or an empty string."""
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
    """Return a user-friendly error message based on HTTP status and API response."""
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
        return normalized


async def fetch_top_headlines(
    country: str = "us",
    category: str = "general",
) -> NewsResponse:
    """Fetch top headlines from NewsData.io with caching."""
    mapped_category = CATEGORY_MAP.get(category, category)
    params: Params = {
        "apikey": NEWS_API_KEY or "",
        "country": country,
        "category": mapped_category,
        "language": "en",
        "size": 10,
    }
    return await _fetch_news(params)


async def search_news(query: str) -> NewsResponse:
    """Search for news articles by keyword using NewsData.io."""
    params: Params = {
        "apikey": NEWS_API_KEY or "",
        "q": query,
        "language": "en",
        "size": 10,
    }
    return await _fetch_news(params)


def generate_summary(article: Article) -> str:
    """Generate a short escaped summary of an article."""
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
    """Format news articles into a readable Telegram-safe message."""
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
    """Format search results into a readable Telegram-safe message."""
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
    """Extract a safe image URL from article if available."""
    url = _safe_url(article.get("urlToImage", ""))
    if url and not url.endswith("null"):
        return url
    return None


async def fetch_breaking_news(countries: list[str], keywords: list[str]) -> list[dict]:
    """Fetch breaking news from multiple countries based on keywords."""
    breaking_articles = []

    # Note: NewsData.io free tier caps `size` at 10 per request, so we fetch
    # 10 articles per country here (was 20 on the old NewsAPI.org free tier).
    for country in countries:
        params = {
            "apikey": NEWS_API_KEY,
            "country": country,
            "language": "en",
            "size": 10,
        }

        cache_key = _get_cache_key(params)
        cached = _get_from_cache(cache_key)
        if cached:
            articles = cached.get("articles", [])
        else:
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.get(NEWS_API_URL, params=params)
                    try:
                        data = response.json()
                    except Exception:
                        continue

                    if response.status_code == 200 and data.get("status") != "error":
                        normalized = _normalize_response(data)
                        _set_cache(cache_key, normalized)
                        articles = normalized.get("articles", [])
                    else:
                        continue
            except Exception:
                continue

        # Filter articles by keywords
        for article in articles:
            title = (article.get("title") or "").lower()
            description = (article.get("description") or "").lower()
            combined = title + " " + description

            if any(keyword.lower() in combined for keyword in keywords):
                article["country"] = country
                breaking_articles.append(article)

    return breaking_articles


def extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from text, filtering common words."""
    common_words = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
        "been", "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "must", "shall", "can",
        "this", "that", "these", "those", "it", "its", "they", "them", "their",
        "he", "him", "his", "she", "her", "hers", "we", "us", "our", "you",
        "your", "i", "me", "my", "what", "which", "who", "whom", "when",
        "where", "why", "how", "if", "then", "else", "while", "after", "before",
        "between", "into", "through", "during", "until", "against", "without",
        "within", "upon", "about", "above", "below", "over", "under", "again",
        "further", "once", "here", "there", "all", "any", "both", "each",
        "few", "more", "most", "other", "some", "such", "no", "nor", "not",
        "only", "own", "same", "so", "than", "too", "very", "just", "now",
        "says", "said", "new", "news", "report", "reports", "update", "updates"
    }

    # Clean and tokenize
    words = text.lower().replace("-", " ").replace("'", " ").split()
    # Filter common words and short words
    keywords = [word for word in words if len(word) > 2 and word not in common_words]
    return keywords


async def fetch_trending_topics(countries: list[str]) -> dict[str, int]:
    """Fetch trending topics across multiple countries."""
    keyword_counts = {}

    for country in countries:
        params = {
            "apikey": NEWS_API_KEY,
            "country": country,
            "language": "en",
            "size": 10,
        }

        cache_key = _get_cache_key(params)
        cached = _get_from_cache(cache_key)
        if cached:
            articles = cached.get("articles", [])
        else:
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.get(NEWS_API_URL, params=params)
                    try:
                        data = response.json()
                    except Exception:
                        continue

                    if response.status_code == 200 and data.get("status") != "error":
                        normalized = _normalize_response(data)
                        _set_cache(cache_key, normalized)
                        articles = normalized.get("articles", [])
                    else:
                        continue
            except Exception:
                continue

        # Extract keywords from titles
        for article in articles:
            title = article.get("title", "") or ""
            keywords = extract_keywords(title)
            for keyword in keywords:
                keyword_counts[keyword] = keyword_counts.get(keyword, 0) + 1

    # Sort by count and return top 10
    sorted_topics = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)
    return dict(sorted_topics[:10])


async def check_api_health() -> dict[str, str]:
    """Check NewsData.io health by making a lightweight test request."""
    params = {
        "apikey": NEWS_API_KEY,
        "country": "us",
        "language": "en",
        "size": 1,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(NEWS_API_URL, params=params, timeout=10.0)

            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "error":
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
            elif response.status_code == 401:
                return {
                    "status": "unhealthy",
                    "error": "Invalid API key",
                }
            elif response.status_code == 429:
                return {
                    "status": "unhealthy",
                    "error": "Rate limit exceeded",
                }
            else:
                return {
                    "status": "unhealthy",
                    "error": f"HTTP {response.status_code}",
                }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
        }
