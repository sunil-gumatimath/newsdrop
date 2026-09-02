"""Unit tests for news_fetcher.py pure helpers and mocked fetch paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from newsdrop import news_fetcher
from newsdrop.news_fetcher import (
    APIClientError,
    _api_country_param,
    _classify_api_error,
    _clean_article_text,
    _escape_text,
    _filter_by_category,
    _get_cache_key,
    _keyword_pattern,
    _normalize_article,
    _normalize_response,
    _query_relevance,
    _query_terms,
    _safe_url,
    _term_pattern,
    _truncate_text,
    extract_keywords,
)

# ── _term_pattern / _keyword_pattern ────────────────────────────────────


def test_term_pattern_whole_word_only():
    pat = _term_pattern("ai")
    assert pat.search("AI is everywhere")
    assert pat.search("the AI revolution")
    # No substring matches inside longer words.
    assert not pat.search("against")
    assert not pat.search("airport")
    assert not pat.search("complaint")
    assert not pat.search("said")


def test_term_pattern_case_insensitive():
    assert _term_pattern("war").search("WAR declared")
    assert _term_pattern("WAR").search("war declared")


def test_keyword_pattern_multiword_phrase():
    pat = _keyword_pattern("Artificial Intelligence")
    assert pat.search("New Artificial Intelligence model")
    assert pat.search("artificial intelligence advances")
    # Partial phrase must not match.
    assert not pat.search("artificial general intelligence")
    assert not pat.search("artificially intelligent")


def test_keyword_pattern_strips_whitespace():
    pat = _keyword_pattern("  earthquake  ")
    assert pat.search("Huge earthquake hits")


# ── _query_terms / _query_relevance ─────────────────────────────────────


def test_query_terms_tokenization():
    assert _query_terms("Bitcoin price") == ["bitcoin", "price"]
    assert _query_terms("") == []
    assert _query_terms("   ") == []
    # Single-char tokens are dropped; punctuation ignored.
    assert _query_terms("a big AI!") == ["big", "ai"]


def test_query_relevance_title_beats_body():
    title_hit = {"title": "OpenAI launches new AI model", "description": "", "content": ""}
    body_hit = {
        "title": "Tech roundup",
        "description": "Lots of talk about ai tools today",
        "content": "",
    }
    assert _query_relevance(title_hit, "AI") > _query_relevance(body_hit, "AI")


def test_query_relevance_no_match_is_zero():
    article = {"title": "Local team wins championship", "description": "Sports result"}
    assert _query_relevance(article, "quantum") == 0.0


def test_query_relevance_empty_query_is_zero():
    article = {"title": "Anything", "description": ""}
    assert _query_relevance(article, "") == 0.0


def test_query_relevance_requires_all_terms():
    # AND semantics: one missing term → no match at all.
    article = {"title": "Bitcoin surges", "description": ""}
    assert _query_relevance(article, "bitcoin ethereum") == 0.0


def test_query_relevance_exact_phrase_bonus():
    phrase = {"title": "Fed signals rate cut soon", "description": "Markets rally"}
    loose = {"title": "Fed rate policy", "description": "cut expectations rise"}
    assert _query_relevance(phrase, "rate cut") > _query_relevance(loose, "rate cut")


# ── _filter_by_category ─────────────────────────────────────────────────


def test_filter_by_category_general_returns_all():
    articles = [{"title": "Anything", "description": ""}]
    assert _filter_by_category(articles, "general") == articles


def test_filter_by_category_unknown_returns_all():
    articles = [{"title": "Anything", "description": ""}]
    assert _filter_by_category(articles, "nosuchcat") == articles


def test_filter_by_category_sports_needs_one_match():
    keep = {"title": "United win the cup final", "description": ""}
    drop = {"title": "Parliament debates budget", "description": "Economy in focus"}
    filtered = _filter_by_category([keep, drop], "sports")
    assert keep in filtered
    assert drop not in filtered


def test_filter_by_category_tech_needs_two_matches():
    one_hit = {"title": "Startup funding round", "description": "A company raised money"}
    two_hits = {"title": "New AI chip unveiled", "description": "The semiconductor device"}
    filtered = _filter_by_category([one_hit, two_hits], "technology")
    assert two_hits in filtered
    assert one_hit not in filtered


def test_filter_by_category_whole_word_semantics():
    # "win" must not match "winter"; single sports keyword won't count.
    article = {"title": "Winter storm blankets the city", "description": "Cold weather"}
    assert _filter_by_category([article], "sports") == []


# ── _clean_article_text / _normalize_article / _normalize_response ──────


def test_clean_article_text_none_and_empty():
    assert _clean_article_text(None) == ""
    assert _clean_article_text("") == ""
    assert _clean_article_text("   ") == ""


def test_clean_article_text_strips_markup_and_entities():
    raw = "<p>Acme &amp; Beta</p><br/>announced <b>merger</b> [+1234 chars]"
    cleaned = _clean_article_text(raw)
    assert "<" not in cleaned
    assert "&" in cleaned
    assert "[+1234 chars]" not in cleaned
    assert cleaned == "Acme & Beta announced merger"


def test_normalize_article_maps_fields():
    raw = {
        "source_id": "srckey",
        "title": "Headline",
        "link": "https://example.com/x",
        "image_url": "https://img.example/1.png",
        "pubDate": "2025-01-01T00:00:00Z",
        "category": ["technology"],
        "country": ["us"],
        "creator": ["Byline"],
        "description": "Short blurb",
        "content": "Long body",
    }
    normalized = _normalize_article(raw)
    assert normalized["source"] == {"name": "srckey"}
    assert normalized["url"] == "https://example.com/x"
    assert normalized["urlToImage"] == "https://img.example/1.png"
    assert normalized["publishedAt"] == "2025-01-01T00:00:00Z"
    assert normalized["category"] == ["technology"]
    assert normalized["country"] == ["us"]
    assert normalized["creator"] == ["Byline"]
    assert normalized["description"] == "Short blurb"


def test_normalize_article_defaults_and_fallbacks():
    normalized = _normalize_article({"title": None})
    assert normalized["source"]["name"] == "Unknown"
    assert normalized["title"] == "No title"
    assert normalized["url"] == ""

    # Missing description falls back to content snippet.
    fallback = _normalize_article({"title": "T", "content": "x" * 600})
    assert fallback["description"] == "x" * 500

    # Non-list category/country coerced to [].
    weird = _normalize_article({"title": "T", "category": "tech", "country": "us"})
    assert weird["category"] == []
    assert weird["country"] == []


def test_normalize_response_shape():
    data = {
        "status": "success",
        "results": [
            {"title": "One", "link": "https://e.com/1"},
            "not-a-dict",
        ],
        "nextPage": "2",
    }
    response = _normalize_response(data)
    assert response["status"] == "success"
    assert len(response["articles"]) == 1
    assert response["nextPage"] == "2"

    bad = _normalize_response({"results": "oops"})
    assert bad["articles"] == []
    assert bad["totalResults"] == 0


# ── _safe_url / _truncate_text / _get_cache_key / misc ──────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a", "https://example.com/a"),
        ("  http://example.com  ", "http://example.com"),
        ("ftp://example.com", ""),
        ("not-a-url", ""),
        ("", ""),
        (None, ""),
        (12345, ""),
    ],
)
def test_safe_url(url, expected):
    assert _safe_url(url) == expected


def test_truncate_text_variants():
    assert _truncate_text("short", 10) == "short"
    long = "a" * 50
    out = _truncate_text(long, 20)
    assert len(out) == 20
    assert out.endswith("...")
    # Trailing whitespace before the ellipsis is stripped.
    assert _truncate_text("abcdef " * 5, 10).endswith("...")


def test_get_cache_key_ignores_apikey_and_sorts():
    key_a = _get_cache_key({"apikey": "secret", "country": "us", "language": "en"})
    key_b = _get_cache_key({"language": "en", "country": "us"})
    assert key_a == key_b == "country=us&language=en"


def test_escape_text_handles_none_and_html():
    assert _escape_text(None) == ""
    assert _escape_text("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"


def test_api_country_param_global_codes():
    assert _api_country_param("world") is None
    assert _api_country_param("GLOBAL") is None
    assert _api_country_param("") is None
    assert _api_country_param(" US ") == "us"


# ── _classify_api_error ─────────────────────────────────────────────────


def test_classify_api_error_status_codes():
    assert "invalid" in _classify_api_error(401, {}).lower()
    assert "invalid" in _classify_api_error(403, {}).lower()
    assert "rate limit" in _classify_api_error(429, {}).lower()
    assert "service" in _classify_api_error(503, {}).lower()


def test_classify_api_error_body_messages():
    msg = _classify_api_error(400, {"status": "error", "message": "Your API key is wrong"})
    assert "API key" in msg

    quota = _classify_api_error(400, {"status": "error", "message": "quota exceeded today"})
    assert "rate limit" in quota.lower()

    generic = _classify_api_error(400, {"status": "error", "message": "something <bad>"})
    assert "something" in generic
    assert "<bad>" not in generic  # HTML-escaped

    code_only = _classify_api_error(400, {"code": "SomeErrorCode"})
    assert "SomeErrorCode" in code_only

    results_str = _classify_api_error(400, {"results": "weird payload"})
    assert "weird payload" in results_str

    fallback = _classify_api_error(400, {})
    assert "unexpected" in fallback.lower()


# ── extract_keywords ────────────────────────────────────────────────────


def test_extract_keywords_drops_stopwords_and_short_words():
    text = "The new AI startup says it will launch a major product update"
    keywords = extract_keywords(text)
    for stop in ("the", "new", "says", "will", "update"):
        assert stop not in keywords
    assert "startup" in keywords
    assert "launch" in keywords
    assert "major" in keywords
    assert "product" in keywords


def test_extract_keywords_normalizes_punctuation():
    # Hyphens and apostrophes become spaces; short tokens dropped.
    keywords = extract_keywords("Breaking well-known CEO's speech")
    assert "breaking" in keywords
    assert "known" in keywords
    assert "ceo" in keywords
    assert "speech" in keywords


# ── HTTP client lifecycle ───────────────────────────────────────────────


async def test_shared_http_client_reused_then_closed():
    client1 = await news_fetcher.get_http_client()
    client2 = await news_fetcher.get_http_client()
    assert client1 is client2
    await news_fetcher.close_http_client()
    assert news_fetcher._http_client is None
    # Creating again after close yields a fresh client.
    client3 = await news_fetcher.get_http_client()
    assert client3 is not client1
    await news_fetcher.close_http_client()


async def test_get_request_count_delegates_to_state():
    with patch(
        "newsdrop.news_fetcher.api_request_count", new_callable=AsyncMock, return_value=(7, 200)
    ):
        used, limit = await news_fetcher.get_request_count()
    assert used == 7
    assert limit == 200


# ── check_api_health (mocked transport) ─────────────────────────────────


def _health_response(status_code: int, payload: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"{}"
    elapsed = MagicMock()
    elapsed.total_seconds.return_value = 0.42
    resp.elapsed = elapsed
    if payload is not None:
        resp.json.return_value = payload
    return resp


async def test_check_api_health_healthy():
    from newsdrop import state as state_mod

    state_mod.reset_backend()
    resp = _health_response(200, {"status": "ok"})
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    with patch.object(news_fetcher, "get_http_client", AsyncMock(return_value=client)):
        result = await news_fetcher.check_api_health()
    assert result["status"] == "healthy"
    assert result["response_time"] == "0.42s"


async def test_check_api_health_cached_result_short_circuits():
    from newsdrop import state as state_mod

    state_mod.reset_backend()
    await state_mod.cache_set("health:api", {"status": "healthy", "response_time": "9s"}, 60)
    client = AsyncMock()
    with patch.object(news_fetcher, "get_http_client", AsyncMock(return_value=client)):
        result = await news_fetcher.check_api_health()
    client.get.assert_not_awaited()
    assert result["response_time"] == "9s"


async def test_check_api_health_error_statuses():
    from newsdrop import state as state_mod

    for status_code, expected_fragment in ((401, "Invalid API key"), (429, "Rate limit")):
        state_mod.reset_backend()
        resp = _health_response(status_code, {"status": "ok"})
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        with patch.object(news_fetcher, "get_http_client", AsyncMock(return_value=client)):
            result = await news_fetcher.check_api_health()
        assert result["status"] == "unhealthy"
        assert expected_fragment in result["error"]

    state_mod.reset_backend()
    resp = _health_response(500, {"status": "ok"})
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    with patch.object(news_fetcher, "get_http_client", AsyncMock(return_value=client)):
        result = await news_fetcher.check_api_health()
    assert "HTTP 500" in result["error"]


async def test_check_api_health_api_error_body_and_exception():
    from newsdrop import state as state_mod

    # status=error body with dict results
    state_mod.reset_backend()
    resp = _health_response(200, {"status": "error", "results": {"message": "boom"}})
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    with patch.object(news_fetcher, "get_http_client", AsyncMock(return_value=client)):
        result = await news_fetcher.check_api_health()
    assert "boom" in result["error"]

    # Transport exception path
    state_mod.reset_backend()
    failing = AsyncMock()
    failing.get = AsyncMock(side_effect=httpx.ConnectError("nope"))
    with patch.object(news_fetcher, "get_http_client", AsyncMock(return_value=failing)):
        result = await news_fetcher.check_api_health()
    assert result["status"] == "unhealthy"
    assert "nope" in result["error"]


# ── fetch_top_headlines / search_news (mocked internals) ────────────────


async def test_fetch_top_headlines_merges_sources_and_reports_them():
    api_articles = [
        {
            "title": "Central bank hikes rates sharply",
            "url": "https://reuters.com/rates",
            "publishedAt": "2025-06-01T12:00:00+00:00",
            "source": {"name": "Reuters"},
        }
    ]
    rss_articles = [
        {
            "title": "Central bank hikes rates sharply higher",
            "url": "https://bbc.com/rates",
            "publishedAt": "2025-06-01T11:00:00+00:00",
            "source": {"name": "BBC"},
        }
    ]
    with (
        patch.object(
            news_fetcher, "_fetch_news", AsyncMock(return_value={"articles": api_articles})
        ),
        patch.object(news_fetcher, "_safe_fetch_rss", AsyncMock(return_value=rss_articles)),
        patch.object(news_fetcher, "_safe_fetch_hn", AsyncMock(return_value=[])),
    ):
        result = await news_fetcher.fetch_top_headlines("us", "general")

    assert result["status"] == "ok"
    assert set(result["sources"]) == {"newsdata.io", "rss"}
    assert 1 <= len(result["articles"]) <= 10
    # Corroborated story ranks first with cluster metadata attached.
    assert result["articles"][0].get("clusterSize") == 2


async def test_fetch_top_headlines_raises_when_both_sources_fail():
    with (
        patch.object(news_fetcher, "_fetch_news", AsyncMock(side_effect=APIClientError("down"))),
        patch.object(news_fetcher, "_safe_fetch_rss", AsyncMock(return_value=[])),
        pytest.raises(APIClientError),
    ):
        await news_fetcher.fetch_top_headlines("us", "general")


async def test_search_news_filters_loose_matches():
    api_articles = [
        {
            "title": "Airport expansion approved",
            "url": "https://e.com/airport",
            "publishedAt": "2025-06-01T12:00:00+00:00",
            "source": {"name": "X"},
        },
        {
            "title": "New AI model beats benchmark",
            "url": "https://e.com/ai",
            "publishedAt": "2025-06-01T13:00:00+00:00",
            "source": {"name": "Y"},
        },
    ]
    with (
        patch.object(
            news_fetcher, "_fetch_news", AsyncMock(return_value={"articles": api_articles})
        ),
        patch.object(news_fetcher, "_safe_fetch_rss", AsyncMock(return_value=[])),
    ):
        result = await news_fetcher.search_news("AI", country="us")

    urls = {a["url"] for a in result["articles"]}
    assert "https://e.com/ai" in urls
    assert "https://e.com/airport" not in urls
    assert result["query"] == "AI"


async def test_search_news_raises_when_nothing_matches_anywhere():
    with (
        patch.object(
            news_fetcher,
            "_fetch_news",
            AsyncMock(side_effect=APIClientError("down")),
        ),
        patch.object(news_fetcher, "_safe_fetch_rss", AsyncMock(return_value=[])),
        pytest.raises(APIClientError),
    ):
        await news_fetcher.search_news("quantum", country="us")


# ── fetch_breaking_news keyword gating ──────────────────────────────────


async def test_fetch_breaking_news_requires_title_hit_or_two_keywords():
    articles = [
        {
            "title": "Calm markets end the week",
            "description": "Stocks drifted sideways",
            "url": "https://e.com/calm",
        },
        {
            "title": "Storm warning issued",
            "description": "Forecasters track the system",
            "url": "https://e.com/storm",
        },
        {
            "title": "Flooded roads after heavy rain",
            "description": "A flood alert and storm damage reported",
            "url": "https://e.com/flood",
        },
    ]
    with (
        patch.object(news_fetcher, "_fetch_news", AsyncMock(return_value={"articles": articles})),
        patch.object(news_fetcher, "_safe_fetch_rss", AsyncMock(return_value=[])),
    ):
        matched = await news_fetcher.fetch_breaking_news(["us"], ["storm", "flood"])

    urls = {a["url"] for a in matched}
    # Title hit qualifies even with a single keyword...
    assert "https://e.com/storm" in urls
    # ...body-only needs >= 2 distinct keyword matches.
    assert "https://e.com/flood" in urls
    assert "https://e.com/calm" not in urls
    # Matched articles are tagged with their country.
    assert all(a["country"] == "us" for a in matched)


async def test_fetch_breaking_news_scans_rss_fallback_too():
    rss_articles = [
        {
            "title": "Earthquake rattles coastal town",
            "description": "",
            "url": "https://rss.example/quake",
        }
    ]
    with (
        patch.object(news_fetcher, "_fetch_news", AsyncMock(return_value={"articles": []})),
        patch.object(news_fetcher, "_safe_fetch_rss", AsyncMock(return_value=rss_articles)),
    ):
        matched = await news_fetcher.fetch_breaking_news(["us"], ["earthquake"])

    assert len(matched) == 1
    assert matched[0]["url"] == "https://rss.example/quake"
    assert matched[0]["country"] == "us"


# ── fetch_trending_topics ───────────────────────────────────────────────


async def test_fetch_trending_topics_counts_keywords():
    articles = [
        {"title": "Solar energy breakthrough announced"},
        {"title": "Solar panel prices fall"},
    ]
    with (
        patch.object(news_fetcher, "_fetch_news", AsyncMock(return_value={"articles": articles})),
        patch.object(news_fetcher, "_safe_fetch_rss", AsyncMock(return_value=[])),
    ):
        topics = await news_fetcher.fetch_trending_topics(["us"], "general")

    assert topics.get("solar") == 2
    assert "energy" in topics


async def test_fetch_trending_topics_survives_country_failure():
    with (
        patch.object(news_fetcher, "_fetch_news", AsyncMock(side_effect=RuntimeError("down"))),
        patch.object(news_fetcher, "_safe_fetch_rss", AsyncMock(return_value=[])),
    ):
        topics = await news_fetcher.fetch_trending_topics(["us", "gb"], "general")
    assert topics == {}
