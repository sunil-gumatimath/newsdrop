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
import random
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import (
    CATEGORY_KEYWORDS,
    DAILY_REQUEST_LIMIT,
    ENABLE_RSS,
    GLOBAL_COUNTRY_CODES,
    NEWS_API_KEY,
)
from .rss_feeds import (
    fetch_rss_articles,
    fetch_rss_category_articles,
    has_rss_category,
    has_rss_for,
)
from .state import (
    api_budget_check,
    api_request_consume,
    api_request_count,
    cache_get,
    cache_set,
)
from .story_ranker import rank_and_cluster

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
# DAILY_REQUEST_LIMIT==0 disables local limiting (unlimited) per config comment.
_request_limit = DAILY_REQUEST_LIMIT

# Shared HTTP client for NewsData.io (connection reuse across fetches).
_http_client: httpx.AsyncClient | None = None
_http_client_lock = asyncio.Lock()


async def get_http_client() -> httpx.AsyncClient:
    """Return a process-wide AsyncClient, creating it on first use."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        return _http_client
    async with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
            _http_client = httpx.AsyncClient(timeout=timeout, max_redirects=5)
        return _http_client


async def close_http_client() -> None:
    """Close the shared client (tests / graceful shutdown)."""
    global _http_client
    async with _http_client_lock:
        if _http_client is not None and not _http_client.is_closed:
            await _http_client.aclose()
        _http_client = None


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


def _clean_article_text(value: object) -> str:
    """Normalize description/content from NewsData into plain readable text."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</div>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # NewsData often appends "[+123 chars]" truncation markers.
    text = re.sub(r"\[\s*\+?\d+\s*chars?\s*\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_article(article: Article) -> Article:
    source_name = article.get("source_name") or article.get("source_id") or "Unknown"
    category = article.get("category", [])
    country = article.get("country", [])

    description = _clean_article_text(article.get("description"))
    content = _clean_article_text(article.get("content"))
    # Prefer a filled description; fall back to content snippet for digests.
    if not description and content:
        description = content[:500]

    return {
        "source": {"name": source_name},
        "title": article.get("title") or "No title",
        "description": description,
        "content": content,
        # Stored URLs are untrusted: validate before persisting downstream.
        "url": _safe_url(article.get("link") or ""),
        "urlToImage": _safe_url(article.get("image_url") or ""),
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

    # 0 means unlimited per config — skip budget gate.
    if _request_limit != 0 and not await api_budget_check(_request_limit):
        current, limit = await get_request_count()
        raise APIClientError(
            f"Daily API request limit reached ({current}/{limit}). "
            "Please try again tomorrow or disable RSS fallback if needed.",
            status_code=429,
            api_code="RateLimitExceeded",
        )

    max_attempts = 3
    last_exc: Exception | None = None
    client = await get_http_client()
    # Consume the API budget once for the whole logical fetch attempt cycle
    # (including retries), so a degraded-API window with retries cannot burn
    # ~3x the free-tier spend. The budget gate above already reserved the
    # request; commit the consumption here.
    for attempt in range(max_attempts):
        try:
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
                # Enforce 2MB response size cap before parsing — check header
                # first to avoid loading large body.
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        if int(str(content_length).strip()) >= 2_000_000:
                            raise APIClientError(
                                "⚠️ The news service returned an unexpectedly large response. "
                                "Please try again later."
                            )
                    except (ValueError, TypeError):
                        pass
                if len(response.content) >= 2_000_000:
                    raise APIClientError(
                        "⚠️ The news service returned an unexpectedly large response. "
                        "Please try again later."
                    )
                data = response.json()
            except APIClientError:
                raise
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
            if _request_limit != 0:
                await api_request_consume(_request_limit)
            return normalized

        except (APIClientError, RuntimeError) as exc:
            last_exc = exc
            # Only retry on 500,502,503,504 and transport errors. 4xx (including 429) never retry.
            status = getattr(exc, "status_code", None)
            if status is not None:
                if 400 <= status < 500:
                    break
                if status not in (500, 502, 503, 504):
                    break
            else:
                # No status code: retry only transport-level APIClientError,
                # not RuntimeError/invalid JSON.
                if isinstance(exc, RuntimeError):
                    break
            if attempt < max_attempts - 1:
                delay = 2**attempt + random.uniform(0, 0.5)  # 1s, 2s + jitter
                logger.warning(
                    "NewsData.io fetch attempt %d/%d failed (%s). Retrying in %.2fs...",
                    attempt + 1,
                    max_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    # Consume the budget exactly once per logical fetch attempt cycle — even
    # when the API was degraded and every attempt failed — so retries cannot
    # multiply free-tier spend.
    if _request_limit != 0:
        await api_request_consume(_request_limit)

    if last_exc is not None:
        raise last_exc
    # Unreachable in practice (max_attempts >= 1 always raises on failure),
    # but mypy needs an explicit terminal statement for the return type.
    raise APIClientError("News fetch failed after retries.")


def _merge_and_dedupe(*sources: list[Article], limit: int = 10) -> list[Article]:
    """Merge sources, cluster near-duplicates, and rank by quality signals.

    Kept as the public merge entrypoint used by tests and call sites. Ranking
    prefers trusted outlets, multi-source corroboration, and freshness over
    pure recency.
    """
    return rank_and_cluster(*sources, limit=limit)


async def _safe_fetch_rss(
    country: str,
    limit: int = 20,
    category: str = "general",
) -> list[Article]:
    """Fetch RSS for country (+ category feeds when available).

    Category-specific feeds are returned as-is (already on-topic). Country
    general feeds are keyword-filtered only when the requested category is
    not general and we had to fall back to them.

    Combined results are capped to ``limit`` (not ``2*limit``) and deduplicated
    by URL so feeds shared between country and category lists are not fetched
    twice.
    """
    if not ENABLE_RSS or not has_rss_for(country, category):
        return []
    try:
        cat = (category or "general").strip().lower()
        if cat and cat != "general" and has_rss_category(cat):
            # Split the limit evenly between category-specific and country feeds
            # so the combined result is capped at ``limit`` (not ``2*limit``).
            half_limit = max(1, limit // 2)
            category_articles, country_articles = await asyncio.gather(
                fetch_rss_category_articles(cat, limit=half_limit),
                fetch_rss_articles(country, limit=half_limit, category="general"),
            )
            # Country general feeds need keyword filter; category feeds do not.
            country_articles = _filter_by_category(country_articles, cat)
            merged: list[Article] = category_articles + country_articles
            # Deduplicate by URL (a feed may appear in both lists).
            seen_urls: set[str] = set()
            deduped: list[Article] = []
            for a in merged:
                url = str(a.get("url", "") or "").strip()
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                deduped.append(a)
            return deduped[:limit]

        articles = await fetch_rss_articles(country, limit=limit, category=cat)
        if cat and cat != "general":
            articles = _filter_by_category(articles, cat)
        return articles
    except Exception as exc:
        logger.warning("RSS fetch failed for %s/%s: %s", country, category, exc)
        return []


def _query_terms(query: str) -> list[str]:
    """Tokenize a search query into matchable terms."""
    raw = (query or "").strip().lower()
    if not raw:
        return []
    # Keep short acronyms (ai, ml, uk) and longer words; drop tiny noise.
    terms = re.findall(r"[a-z0-9]{2,}", raw)
    return terms


def _term_pattern(term: str) -> re.Pattern[str]:
    """Whole-word / whole-token pattern.

    For 2-letter terms like ``ai``, require a real word boundary so we do
    **not** match inside ``against``, ``airport``, ``complaint``, ``said``.
    """
    escaped = re.escape(term.lower())
    # Word boundary: letters/digits on either side block the match.
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)


def _keyword_pattern(kw: str) -> re.Pattern[str]:
    """Whole-word pattern for breaking keywords, supports multi-word phrases.

    Uses the same lookaround logic as ``_term_pattern`` so phrases like
    ``artificial intelligence`` only match as a complete phrase and do not
    trigger on substrings.
    """
    return _term_pattern(kw.strip().lower())


def _article_text_fields(article: Article) -> tuple[str, str]:
    title = str(article.get("title", "") or "")
    body = f"{article.get('description', '') or ''} {article.get('content', '') or ''}"
    return title, body


def _query_relevance(article: Article, query: str) -> float:
    """Score how well an article matches the query (0 = no match).

    Title hits beat body hits. Multi-term queries need every term somewhere
    (AND), but score higher when more terms land in the title.
    """
    terms = _query_terms(query)
    if not terms:
        return 0.0

    title, body = _article_text_fields(article)
    title_l = title.lower()
    body_l = body.lower()
    blob_l = f"{title_l} {body_l}"

    score = 0.0
    for term in terms:
        pat = _term_pattern(term)
        in_title = bool(pat.search(title_l))
        in_body = bool(pat.search(body_l))
        if not in_title and not in_body:
            # Phrase fallback only for multi-word original query on full blob.
            return 0.0
        if in_title:
            # Short acronyms in title are strong signals (AI, ML, ...).
            score += 4.0 if len(term) <= 3 else 3.0
        elif in_body:
            score += 1.0

    # Exact phrase bonus (e.g. "artificial intelligence").
    phrase = (query or "").strip().lower()
    if len(phrase) >= 4 and phrase in blob_l:
        score += 2.0
        if phrase in title_l:
            score += 3.0

    return score


def _filter_by_query(articles: list[Article], query: str) -> list[Article]:
    """Keep articles that match the query with whole-word semantics."""
    if not (query or "").strip():
        return articles
    ranked: list[tuple[float, Article]] = []
    for article in articles:
        score = _query_relevance(article, query)
        if score > 0:
            ranked.append((score, article))
    ranked.sort(
        key=lambda item: (item[0], str(item[1].get("publishedAt", "") or "")),
        reverse=True,
    )
    return [article for _, article in ranked]


def _filter_by_category(articles: list[Article], category: str) -> list[Article]:
    if category == "general" or category not in CATEGORY_KEYWORDS:
        return articles

    keywords = CATEGORY_KEYWORDS[category]
    min_matches = 1 if category == "sports" else 2

    # Whole-word matching so short keywords don't match inside other words
    # (e.g. "win" must not match "winter"/"window", "ai" must not match
    # "against", "race" must not match "brace"). Mirrors the whole-word
    # semantics used by the search and breaking-alert paths.
    patterns = [_term_pattern(kw) for kw in keywords]

    filtered: list[Article] = []
    for article in articles:
        title = str(article.get("title", "")).lower()
        description = str(article.get("description", "")).lower()
        blob = f"{title} {description}"
        match_count = sum(1 for pat in patterns if pat.search(blob))

        if match_count >= min_matches:
            filtered.append(article)

    return filtered


def _api_country_param(country: str) -> str | None:
    """Return NewsData country code, or None for world/global feeds."""
    code = (country or "").strip().lower()
    if not code or code in GLOBAL_COUNTRY_CODES:
        return None
    return code


def _api_language_param(language: str | None) -> str | None:
    """Return NewsData language code, or None when 'all' / empty (no filter)."""
    if language is None:
        return "en"
    code = str(language).strip().lower()
    if not code or code == "all":
        return None
    return code


async def fetch_top_headlines(
    country: str = "us",
    category: str = "general",
    language: str = "en",
) -> NewsResponse:
    mapped_category = CATEGORY_MAP.get(category, category)
    params: Params = {
        "apikey": NEWS_API_KEY or "",
        "size": 10,
    }
    lang = _api_language_param(language)
    if lang:
        params["language"] = lang
    api_country = _api_country_param(country)
    if api_country:
        params["country"] = api_country

    if mapped_category and mapped_category != "top":
        params["category"] = mapped_category

    api_result, rss_result = await asyncio.gather(
        _fetch_news(params),
        _safe_fetch_rss(country, limit=20, category=category),
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
    elif isinstance(rss_result, list):
        rss_articles = rss_result

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


async def search_news(query: str, country: str = "us", language: str = "en") -> NewsResponse:
    q = (query or "").strip()
    params: Params = {
        "apikey": NEWS_API_KEY or "",
        "q": q,
        "size": 10,
    }
    lang = _api_language_param(language)
    if lang:
        params["language"] = lang

    api_data: NewsResponse | None = None
    api_error: Exception | None = None

    try:
        api_data = await _fetch_news(params)
    except Exception as exc:
        api_error = exc
        logger.warning("NewsData.io search failed, will try RSS: %s", exc)

    # RSS: pull a wider pool, then strict whole-word filter (fixes "AI" → airport).
    rss_articles = await _safe_fetch_rss(country, limit=50, category="general")
    rss_articles = _filter_by_query(rss_articles, q)

    api_articles = (api_data or {}).get("articles", [])
    api_articles = api_articles if isinstance(api_articles, list) else []
    # API can still return loose matches for short queries — enforce relevance.
    api_articles = _filter_by_query(api_articles, q)

    if not api_articles and not rss_articles and api_error is not None:
        raise api_error

    # Prefer API hits (already query-targeted), then RSS; re-score by relevance.
    merged = _merge_and_dedupe(api_articles, rss_articles, limit=20)
    merged = _filter_by_query(merged, q)[:10]

    return {
        "status": "ok",
        "totalResults": len(merged),
        "articles": merged,
        "query": q,
    }


async def fetch_breaking_news(
    countries: list[str], keywords: list[str], language: str = "en"
) -> list[Article]:
    """Fetch breaking news from the NewsData.io API, with RSS fallback.

    The budget gate protects the API. RSS feeds have zero API cost, so we
    always check them — if the API is down or the budget is exhausted,
    RSS headlines are still scanned for keyword matches.
    """
    breaking_articles: list[Article] = []

    for country in countries:
        # Rate-limit gate: if the daily budget is gone, log once and bail out
        # of the whole country loop (do not "continue" — remaining countries
        # would just hit the same gate and spam the log).
        # 0 == unlimited, so skip gate entirely.
        if _request_limit != 0 and not await api_budget_check(_request_limit):
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
            "size": 10,
        }
        lang = _api_language_param(language)
        if lang:
            params["language"] = lang
        api_country = _api_country_param(country)
        if api_country:
            params["country"] = api_country

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
                # API failed for this country — articles stays empty; we'll
                # fall through to RSS below.
                articles = []

        for article in articles:
            title = str(article.get("title", "")).lower()
            description = str(article.get("description", "")).lower()
            combined = f"{title} {description}"

            matched = [kw for kw in keywords if _keyword_pattern(kw).search(combined)]
            title_hits = [kw for kw in matched if _keyword_pattern(kw).search(title)]
            if title_hits or len(matched) >= 2:
                tagged = dict(article)
                tagged["country"] = country
                breaking_articles.append(tagged)

        # RSS fallback: zero-cost scan of feeds for the same country.
        # If the API returned nothing or failed entirely, RSS may still have
        # breaking headlines. We apply the same keyword+tagging logic so
        # RSS-only breaking articles are indistinguishable from API ones.
        rss_articles = await _safe_fetch_rss(country, limit=20, category="general")
        for article in rss_articles:
            title = str(article.get("title", "")).lower()
            description = str(article.get("description", "")).lower()
            combined = f"{title} {description}"

            matched = [kw for kw in keywords if _keyword_pattern(kw).search(combined)]
            title_hits = [kw for kw in matched if _keyword_pattern(kw).search(title)]
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


async def _fetch_trending_for_country(
    country: str,
    mapped_category: str,
    category: str,
    keyword_counts: dict[str, int],
    semaphore: asyncio.Semaphore | None = None,
) -> None:
    """Fetch articles for a single country and update keyword_counts in-place.

    Extracted so multiple countries can be fetched concurrently via gather().
    Pass a semaphore to throttle how many countries fetch in flight at once.
    """
    if semaphore is not None:
        await semaphore.acquire()
    try:
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
                return

        rss_articles = await _safe_fetch_rss(country, limit=20, category=category)
        articles = _merge_and_dedupe(articles, rss_articles, limit=20)

        for article in articles:
            title = str(article.get("title", "") or "")
            for keyword in extract_keywords(title):
                keyword_counts[keyword] = keyword_counts.get(keyword, 0) + 1
    finally:
        if semaphore is not None:
            semaphore.release()


async def fetch_trending_topics(countries: list[str], category: str = "general") -> dict[str, int]:
    keyword_counts: dict[str, int] = {}
    mapped_category = CATEGORY_MAP.get(category, category)

    # Fetch all countries concurrently. Each task acquires the semaphore
    # internally, so concurrency is throttled to 3 in flight at once rather
    # than hammering the API with all requests when many countries are
    # configured (9 countries × up to 2 requests each = 18 API calls).
    semaphore = asyncio.Semaphore(3)
    tasks = [
        asyncio.create_task(
            _fetch_trending_for_country(
                country, mapped_category, category, keyword_counts, semaphore
            )
        )
        for country in countries
    ]
    await asyncio.gather(*tasks)

    sorted_topics = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)
    return dict(sorted_topics[:10])


async def check_api_health() -> dict[str, str]:
    cached = await cache_get("health:api")
    if isinstance(cached, dict):
        return cached

    params: Params = {
        "apikey": NEWS_API_KEY or "",
        "country": "us",
        "language": "en",
        "size": 1,
    }

    try:
        client = await get_http_client()
        # Health checks use a shorter timeout than normal fetches.
        response = await client.get(
            NEWS_LATEST_URL,
            params=params,
            timeout=httpx.Timeout(10.0),
        )

        if response.status_code == 200:
            # Enforce 2MB response size cap
            if len(response.content) >= 2_000_000:
                result = {"status": "unhealthy", "error": "Response too large (>=2MB)"}
                await cache_set("health:api", result, 60)
                return result
            data = response.json()
            if isinstance(data, dict) and data.get("status") == "error":
                results = data.get("results")
                if isinstance(results, dict):
                    err = results.get("message", "Unknown error")
                else:
                    err = data.get("message", "Unknown error")
                result = {"status": "unhealthy", "error": f"API error: {err}"}
                await cache_set("health:api", result, 60)
                return result

            result = {
                "status": "healthy",
                "response_time": f"{response.elapsed.total_seconds():.2f}s",
            }
            await cache_set("health:api", result, 60)
            return result

        if response.status_code == 401:
            result = {"status": "unhealthy", "error": "Invalid API key"}
        elif response.status_code == 429:
            result = {"status": "unhealthy", "error": "Rate limit exceeded"}
        else:
            result = {"status": "unhealthy", "error": f"HTTP {response.status_code}"}
        await cache_set("health:api", result, 60)
        return result
    except Exception as exc:
        result = {"status": "unhealthy", "error": str(exc)}
        await cache_set("health:api", result, 60)
        return result
