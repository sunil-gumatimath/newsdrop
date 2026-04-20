import asyncio
import html
import logging
import re
import httpx
from datetime import datetime, timezone
from config import (
    NEWS_API_KEY,
    NEWS_API_URL,
    NEWSDATA_CATEGORY_MAP,
    ENABLE_RSS,
)
from rss_feeds import fetch_rss_articles, has_rss_for

logger = logging.getLogger(__name__)


def _newsdata_date_to_iso(raw: str) -> str:
    """Convert NewsData.io's 'YYYY-MM-DD HH:MM:SS' (UTC) into ISO 8601 with tz.

    Returns the raw value unchanged if it can't be parsed. This makes article
    publishedAt timestamps sortable as strings across sources (RSS uses ISO 8601
    with tz offset, so we standardize on that format everywhere).
    """
    if not raw:
        return ""
    # Already ISO-ish (contains 'T' or a tz offset) → trust it
    if "T" in raw or raw.endswith("Z") or "+" in raw[10:]:
        return raw
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return raw


def format_relative_time(published_at: str) -> str:
    if not published_at:
        return ""
    try:
        # NewsData.io pubDate format: "2024-01-15 12:34:56" (UTC, no tz).
        # Also handle ISO 8601 with Z suffix for safety.
        pub_str = published_at.replace("Z", "+00:00")
        try:
            pub_time = datetime.fromisoformat(pub_str)
        except ValueError:
            # Fallback for "YYYY-MM-DD HH:MM:SS" (NewsData.io UTC format)
            pub_time = datetime.strptime(published_at, "%Y-%m-%d %H:%M:%S")
            pub_time = pub_time.replace(tzinfo=timezone.utc)

        if pub_time.tzinfo is None:
            pub_time = pub_time.replace(tzinfo=timezone.utc)

        now = datetime.now(pub_time.tzinfo)
        delta = now - pub_time
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return "just now"
        elif total_seconds < 3600:
            mins = total_seconds // 60
            return f"{mins}m ago"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours}h ago"
        elif total_seconds < 172800:
            return "yesterday"
        else:
            days = total_seconds // 86400
            return f"{days}d ago"
    except Exception:
        return ""


# Simple in-memory cache: {cache_key: (timestamp, data)}
_cache: dict[str, tuple[datetime, dict]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes


def _get_cache_key(params: dict) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "apikey")


def _get_from_cache(key: str) -> dict | None:
    if key in _cache:
        ts, data = _cache[key]
        if (datetime.now() - ts).total_seconds() < CACHE_TTL_SECONDS:
            return data
        del _cache[key]
    return None


def _set_cache(key: str, data: dict) -> None:
    _cache[key] = (datetime.now(), data)


def _normalize_article(item: dict) -> dict:
    """Convert a NewsData.io article into the NewsAPI-style shape the bot expects.

    NewsData.io fields → NewsAPI-style fields:
      title        → title
      link         → url
      description  → description
      content      → content
      image_url    → urlToImage
      pubDate      → publishedAt  (converted to ISO 8601 UTC)
      source_id / source_name → source.name
    """
    source_name = (
        item.get("source_name")
        or item.get("source_id")
        or "Unknown"
    )
    return {
        "title": item.get("title") or "No title",
        "url": item.get("link") or "",
        "description": item.get("description") or "",
        "content": item.get("content") or "",
        "urlToImage": item.get("image_url") or "",
        # Normalize to ISO 8601 so cross-source sort works correctly with RSS.
        "publishedAt": _newsdata_date_to_iso(item.get("pubDate") or ""),
        "source": {"name": source_name},
    }


def _normalize_response(data: dict) -> dict:
    """Convert a NewsData.io response to NewsAPI-style: {status, articles, totalResults}."""
    results = data.get("results") or []
    articles = [_normalize_article(item) for item in results]
    return {
        "status": "ok",
        "totalResults": data.get("totalResults", len(articles)),
        "articles": articles,
    }


def _classify_api_error(status_code: int, body: dict) -> str:
    """Return a user-friendly error message based on HTTP status and API response.

    NewsData.io error shape (on failure):
      { "status": "error", "results": { "code": "...", "message": "..." } }
    """
    results = body.get("results")
    if isinstance(results, dict):
        code = results.get("code", "")
        msg = results.get("message", "")
    else:
        code = body.get("code", "")
        msg = body.get("message", "")

    if status_code == 401 or code in ("Unauthorized", "InvalidApiKey"):
        return "⚠️ The news API key is invalid. Please check your configuration."
    if status_code == 429 or code == "RateLimitExceeded":
        return (
            "⏳ The news API rate limit has been reached. "
            "Please try again in a few minutes."
        )
    if status_code == 403 or code in ("CreditsExhausted", "Forbidden"):
        return "⚠️ The news API quota has been exhausted. Contact the bot admin."
    if status_code == 422:
        return f"⚠️ Invalid request to news API: {msg or 'bad parameter'}"
    if status_code >= 500:
        return "🔧 The news service is experiencing issues. Please try again later."
    if msg:
        return f"⚠️ News API error: {msg}"
    return "⚠️ An unexpected error occurred while fetching news. Please try again later."


async def _fetch_newsdata(params: dict) -> dict:
    """Core fetch against NewsData.io with caching. Returns a NewsAPI-style dict."""
    cache_key = _get_cache_key(params)
    cached = _get_from_cache(cache_key)
    if cached:
        return cached

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(NEWS_API_URL, params=params)
        try:
            data = response.json()
        except Exception:
            raise RuntimeError(
                f"News API returned invalid response (HTTP {response.status_code})"
            )

        if response.status_code != 200 or data.get("status") == "error":
            raise APIClientError(
                _classify_api_error(response.status_code, data),
                status_code=response.status_code,
                api_code=(
                    data.get("results", {}).get("code")
                    if isinstance(data.get("results"), dict)
                    else data.get("code")
                ),
            )

        normalized = _normalize_response(data)
        _set_cache(cache_key, normalized)
        return normalized


# ── Article dedup / merge helpers ────────────────────────────────────

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalize_title(title: str) -> str:
    """Lowercase + strip non-alphanumerics for fuzzy title comparison."""
    if not title:
        return ""
    return " ".join(_WORD_RE.findall(title.lower()))


def _merge_and_dedupe(*sources: list[dict], limit: int = 10) -> list[dict]:
    """Merge article lists from multiple sources, dedupe by URL + normalized title,
    and sort by publishedAt (newest first).
    """
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    merged: list[dict] = []

    for source in sources:
        for article in source:
            url = (article.get("url") or "").strip()
            title_key = _normalize_title(article.get("title") or "")

            # Skip if URL already seen OR title already seen (and non-empty)
            if url and url in seen_urls:
                continue
            if title_key and title_key in seen_titles:
                continue

            if url:
                seen_urls.add(url)
            if title_key:
                seen_titles.add(title_key)
            merged.append(article)

    # Sort by publishedAt desc. Articles with no date sink to the bottom.
    merged.sort(key=lambda a: a.get("publishedAt", "") or "", reverse=True)
    return merged[:limit]


async def _safe_fetch_rss(country: str, limit: int = 20) -> list[dict]:
    """Fetch RSS articles, swallowing all errors so RSS never breaks the request."""
    if not ENABLE_RSS or not has_rss_for(country):
        return []
    try:
        return await fetch_rss_articles(country, limit=limit)
    except Exception as e:
        logger.warning("RSS fetch failed for %s: %s", country, e)
        return []


def _filter_by_query(articles: list[dict], query: str) -> list[dict]:
    """Simple case-insensitive substring filter over title + description."""
    q = query.lower().strip()
    if not q:
        return articles
    result = []
    for a in articles:
        blob = (a.get("title") or "") + " " + (a.get("description") or "")
        if q in blob.lower():
            result.append(a)
    return result


# ── Public fetchers ──────────────────────────────────────────────────

async def fetch_top_headlines(country: str = "in", category: str = "general") -> dict:
    """Fetch top headlines from NewsData.io + RSS sources with caching and dedup.

    Behaviour:
      • Always tries both sources in parallel when RSS is enabled.
      • If the NewsData.io call fails (rate-limited, quota exhausted, etc.) but
        RSS succeeds, we still return RSS-only results instead of erroring out.
      • If both sources fail, the original NewsData.io APIClientError is re-raised.
      • For non-'general' categories, RSS results are loosely filtered by category
        keyword since most RSS feeds are general-purpose.
    """
    params = {
        "apikey": NEWS_API_KEY,
        "country": country,
        "language": "en",
        "size": 10,
    }

    # Map user category → NewsData.io category. Skip category param for "general/top"
    # to maximize coverage, or include it if you want strict topical filtering.
    nd_category = NEWSDATA_CATEGORY_MAP.get(category, "top")
    if nd_category and nd_category != "top":
        params["category"] = nd_category

    # Fetch both sources concurrently so we can tolerate partial failures
    # (e.g. API fails, RSS succeeds).
    api_task = asyncio.create_task(_fetch_newsdata(params))
    rss_task = asyncio.create_task(_safe_fetch_rss(country, limit=20))

    api_data: dict | None = None
    api_error: Exception | None = None
    try:
        api_data = await api_task
    except APIClientError as e:
        api_error = e
        logger.warning("NewsData.io fetch failed, will try RSS: %s", e)
    except Exception as e:
        api_error = e
        logger.warning("Unexpected NewsData.io error, will try RSS: %s", e)

    rss_articles = await rss_task

    # Note: RSS feeds are general-purpose and don't expose category filtering.
    # For non-'general' categories we rely on NewsData.io's category filter
    # for topical precision and keep RSS as a breadth/freshness booster.

    api_articles = (api_data or {}).get("articles", [])

    # If both sources returned nothing AND the API errored, re-raise.
    if not api_articles and not rss_articles and api_error is not None:
        raise api_error

    merged = _merge_and_dedupe(api_articles, rss_articles, limit=10)

    sources_used = []
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


async def search_news(query: str, country: str = "in") -> dict:
    """Search for news by keyword across NewsData.io + RSS with dedup.

    RSS feeds don't expose a search API, so we fetch the country's RSS firehose
    and substring-filter locally.
    """
    params = {
        "apikey": NEWS_API_KEY,
        "q": query,
        "language": "en",
        "size": 10,
    }

    api_task = asyncio.create_task(_fetch_newsdata(params))
    rss_task = asyncio.create_task(_safe_fetch_rss(country, limit=50))

    api_data: dict | None = None
    api_error: Exception | None = None
    try:
        api_data = await api_task
    except APIClientError as e:
        api_error = e
        logger.warning("NewsData.io search failed, will try RSS: %s", e)
    except Exception as e:
        api_error = e
        logger.warning("Unexpected NewsData.io search error, will try RSS: %s", e)

    rss_articles = await rss_task
    rss_articles = _filter_by_query(rss_articles, query)

    api_articles = (api_data or {}).get("articles", [])

    if not api_articles and not rss_articles and api_error is not None:
        raise api_error

    merged = _merge_and_dedupe(api_articles, rss_articles, limit=10)

    return {
        "status": "ok",
        "totalResults": len(merged),
        "articles": merged,
    }


class APIClientError(Exception):
    """Custom exception for NewsData.io errors with structured info."""

    def __init__(self, message: str, status_code: int = None, api_code: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code


def generate_summary(article: dict) -> str:
    """Generate a 2-3 sentence summary of an article."""
    title = article.get("title", "No title")
    description = article.get("description", "")
    content = article.get("content", "")

    summary = description or content

    if not summary:
        return title

    # Truncate to ~2-3 sentences
    sentences = summary.replace("..", ".").split(". ")
    short = ". ".join(sentences[:3])

    if len(short) > 200:
        short = short[:197] + "..."

    return short


def format_briefing(data: dict, country: str = "in", category: str = "general") -> str:
    """Format news articles into a readable Telegram message."""
    articles = data.get("articles", [])

    if not articles:
        return (
            f"No {category} news articles found for {country.upper()}. Try again later."
        )

    published_at = articles[0].get("publishedAt", "") if articles else ""
    date_str = (
        published_at[:10] if published_at else datetime.now().strftime("%Y-%m-%d")
    )

    cat_label = category.capitalize() if category != "general" else "Top"
    message = f"📰 <b>Daily News Briefing — {date_str}</b>\n🌍 {cat_label} Headlines ({country.upper()})\n\n"

    for i, article in enumerate(articles[:10], 1):
        source = article.get("source", {}).get("name", "Unknown")
        title = article.get("title", "No title")
        url = article.get("url", "")
        description = article.get("description", "")
        pub_time = article.get("publishedAt", "")

        time_label = f" · {format_relative_time(pub_time)}" if pub_time else ""
        message += f'<b>{i}. <a href="{url}">{title}</a></b>{time_label}\n'
        if description:
            desc = description[:150] + "..." if len(description) > 150 else description
            message += f"<i>{desc}</i>\n"
        message += f"📍 {source}\n\n"

    message += "Stay informed! 🌍"
    return message


def format_search_results(data: dict, query: str) -> str:
    """Format search results into a readable Telegram message."""
    articles = data.get("articles", [])

    if not articles:
        return (
            f'No results found for "{html.escape(query)}". Try a different search term.'
        )

    message = f'🔍 <b>Search Results: "{html.escape(query)}"</b>\n\n'

    for i, article in enumerate(articles[:10], 1):
        source = article.get("source", {}).get("name", "Unknown")
        title = article.get("title", "No title")
        url = article.get("url", "")
        summary = generate_summary(article)

        message += f'<b>{i}. <a href="{url}">{title}</a></b>\n'
        message += f"<i>{summary}</i>\n"
        message += f"📍 {source}\n\n"

    message += f"Found {data.get('totalResults', 0)} results. Showing top 10."
    return message


def get_article_image(article: dict) -> str | None:
    """Extract image URL from article if available."""
    url = article.get("urlToImage", "")
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
