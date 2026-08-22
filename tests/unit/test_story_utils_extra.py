"""Additional unit tests for story_utils.py and story_ranker.py."""

from __future__ import annotations

from datetime import UTC, datetime

from newsdrop.story_ranker import (
    _cluster_score,
    _recency_score,
    cluster_articles,
    pick_representative,
    rank_and_cluster,
    source_trust,
)
from newsdrop.story_utils import (
    canonical_url,
    normalize_title,
    same_story,
    source_name,
    title_similarity,
    titles_are_similar,
)

# ── canonical_url ───────────────────────────────────────────────────────


def test_canonical_url_strips_utm_and_tracking():
    url = "https://example.com/story?utm_source=rss&utm_medium=feed&id=7"
    assert canonical_url(url) == "https://example.com/story?id=7"


def test_canonical_url_strips_ref_fbclid_gclid():
    url = "https://example.com/a?ref=tw&fbclid=xyz&gclid=abc&q=1"
    assert canonical_url(url) == "https://example.com/a?q=1"


def test_canonical_url_normalizes_host_and_trailing_slash():
    assert canonical_url("https://WWW.Example.com/path/") == "https://example.com/path"
    # Root path collapses to "/".
    assert canonical_url("https://example.com/") == "https://example.com/"


def test_canonical_url_rejects_bad_input():
    assert canonical_url("") == ""
    assert canonical_url("   ") == ""
    assert canonical_url("ftp://example.com/x") == ""
    assert canonical_url("not-a-url") == ""


def test_canonical_url_sorts_query_params():
    url = "https://example.com/s?b=2&a=1"
    assert canonical_url(url) == "https://example.com/s?a=1&b=2"


# ── titles_are_similar branches ─────────────────────────────────────────


def test_titles_similar_empty_side_is_false():
    assert titles_are_similar("", "Some headline text here") is False
    assert titles_are_similar("Some headline text here", "") is False


def test_titles_similar_identical_normalized_short_titles():
    # Both below min_chars but equal after normalization → True.
    assert titles_are_similar("Hi!", "hi")


def test_titles_similar_short_but_different_is_false():
    assert titles_are_similar("Hi", "Bye") is False


def test_titles_similar_substring_of_long_titles():
    long = "The government announced a major new climate policy framework today"
    short = "government announced a major new climate policy"
    assert titles_are_similar(long, short) is True


def test_titles_similar_jaccard_fallback():
    left = "Fed raises interest rates by 25 basis points in March"
    right = "Fed raises interest rates 25 basis points March decision"
    assert titles_are_similar(left, right) is True
    assert not titles_are_similar("Completely different topic about sports", left)


def test_title_similarity_bounds():
    assert title_similarity("", "x y z") == 0.0
    assert title_similarity("alpha beta", "alpha beta gamma") > 0.5
    assert title_similarity("alpha beta", "delta epsilon") == 0.0


def test_normalize_title_lowercases_and_drops_punctuation():
    assert normalize_title("Hello, World! 123") == "hello world 123"
    assert normalize_title("") == ""


# ── same_story / source_name ────────────────────────────────────────────


def test_same_story_matches_via_canonical_url():
    left = {"url": "https://example.com/x?utm_source=a", "title": "One headline"}
    right = {"url": "https://www.example.com/x", "title": "Totally different words"}
    assert same_story(left, right) is True


def test_same_story_matches_via_title_when_urls_differ():
    left = {"url": "https://a.com/1", "title": "Earthquake strikes coastal region killing dozens"}
    right = {"url": "https://b.com/2", "title": "Earthquake strikes coastal region killing dozens"}
    assert same_story(left, right) is True


def test_same_story_no_match():
    left = {"url": "https://a.com/1", "title": "Sports team wins the final match"}
    right = {"url": "https://b.com/2", "title": "Scientists discover a new exoplanet"}
    assert same_story(left, right) is False


def test_source_name_variants():
    assert source_name({"source": {"name": "Reuters"}}) == "Reuters"
    assert source_name({"source": "BBC"}) == "BBC"
    assert source_name({}) == ""
    assert source_name({"source": None}) == ""


# ── source_trust / recency / cluster score ──────────────────────────────


def test_source_trust_word_boundary_no_false_positives():
    # "nyt" must not match inside "anytime".
    assert source_trust("Anytime News") == 0.5
    assert source_trust("bbcworld") == 0.5
    assert source_trust("The New York Times") >= 0.9


def test_recency_score_unknown_date_scores_low():
    assert _recency_score({}) == 0.25
    assert _recency_score({"publishedAt": "not-a-date"}) == 0.25


def test_recency_score_fresh_beats_old():
    now = datetime(2025, 6, 2, tzinfo=UTC)
    fresh = _recency_score({"publishedAt": "2025-06-02T00:00:00+00:00"}, now=now)
    day_old = _recency_score({"publishedAt": "2025-06-01T00:00:00+00:00"}, now=now)
    ancient = _recency_score({"publishedAt": "2024-01-01T00:00:00+00:00"}, now=now)
    assert fresh > day_old > ancient
    assert ancient == 0.0


def test_cluster_score_saturates_after_four_outlets():
    base = {
        "title": "Story",
        "publishedAt": "2025-06-01T00:00:00+00:00",
        "source": {"name": "Reuters"},
    }
    small = dict(base, clusterSize=2)
    big = dict(base, clusterSize=9)
    score_small = _cluster_score(small, now=datetime(2025, 6, 2, tzinfo=UTC))
    score_big = _cluster_score(big, now=datetime(2025, 6, 2, tzinfo=UTC))
    # Diminishing returns: 8 outlets only beat 2 by the recency/trust margin.
    assert score_big > score_small
    assert score_big - score_small < 1.6  # max extra corroboration is capped


# ── clustering + representative tie-breakers ────────────────────────────


def test_cluster_articles_order_preserving_and_greedy():
    articles = [
        {"title": "Alpha story headline one", "url": "https://a.com/1"},
        {"title": "Beta story headline two", "url": "https://b.com/2"},
        {"title": "Alpha story headline one", "url": "https://c.com/3"},
    ]
    clusters = cluster_articles(articles)
    assert len(clusters) == 2
    assert clusters[0] == [articles[0], articles[2]]
    assert clusters[1] == [articles[1]]


def test_pick_representative_prefers_usable_blurb_on_tie():
    now = datetime(2025, 6, 2, tzinfo=UTC)
    no_blurb = {
        "title": "Same headline for both stories here",
        "url": "https://a.com/1",
        "description": "",
        "publishedAt": "2025-06-01T12:00:00+00:00",
        "source": {"name": "Random Blog One"},
    }
    with_blurb = {
        "title": "Same headline for both stories here",
        "url": "https://b.com/2",
        "description": "A real description that is definitely longer than forty characters.",
        "publishedAt": "2025-06-01T12:00:00+00:00",
        "source": {"name": "Random Blog Two"},
    }
    rep = pick_representative([no_blurb, with_blurb], now=now)
    # Equal trust + recency → the article with a usable blurb wins.
    assert rep["url"] == "https://b.com/2"
    assert rep["clusterSize"] == 2
    assert set(rep["relatedSources"]) == {"Random Blog One", "Random Blog Two"} - {
        rep["source"]["name"]
    }


def test_pick_representative_single_article_gets_defaults():
    rep = pick_representative([{"title": "Solo", "url": "https://a.com/1"}])
    assert rep["clusterSize"] == 1
    assert rep["relatedSources"] == []


def test_pick_representative_related_sources_capped_at_five():
    cluster = [
        {
            "title": "Big corroborated breaking news headline",
            "url": f"https://s{i}.com/{i}",
            "source": {"name": f"Outlet{i}"},
        }
        for i in range(8)
    ]
    rep = pick_representative(cluster)
    assert len(rep["relatedSources"]) <= 5
    assert rep["clusterSize"] == 8


def test_rank_and_cluster_empty_input():
    assert rank_and_cluster() == []
    assert rank_and_cluster([], []) == []


def test_rank_and_cluster_respects_limit():
    # Lexically distinct titles so nothing clusters together.
    articles = [
        {
            "title": f"alpha{i} beta{i} gamma{i} delta{i}",
            "url": f"https://e.com/{i}",
        }
        for i in range(15)
    ]
    ranked = rank_and_cluster(articles, limit=5)
    assert len(ranked) == 5


def test_rank_and_cluster_end_to_end_metadata():
    now = datetime(2025, 1, 3, tzinfo=UTC)
    api = [
        {
            "title": "Storm batters the coastline overnight",
            "url": "https://reuters.com/storm",
            "publishedAt": "2025-01-02T10:00:00+00:00",
            "source": {"name": "Reuters"},
        },
        {
            "title": "Quiet day in markets as traders wait",
            "url": "https://blog.example/markets",
            "publishedAt": "2025-01-02T09:00:00+00:00",
            "source": {"name": "Random Blog"},
        },
    ]
    rss = [
        {
            "title": "Storm batters the coastline overnight across region",
            "url": "https://bbc.co.uk/storm",
            "publishedAt": "2025-01-02T09:30:00+00:00",
            "source": {"name": "BBC"},
        }
    ]
    ranked = rank_and_cluster(api, rss, limit=10, now=now)
    assert len(ranked) == 2
    storm = next(a for a in ranked if "storm" in a["url"])
    assert storm["clusterSize"] == 2
    assert "BBC" in storm["relatedSources"]
    # The corroborated Reuters story outranks the lone blog post.
    assert ranked[0]["url"] == "https://reuters.com/storm"
