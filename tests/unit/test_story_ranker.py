"""Tests for story clustering, source trust, and ranking."""

from __future__ import annotations

from datetime import UTC, datetime

from newsdrop.story_ranker import (
    cluster_articles,
    pick_representative,
    rank_and_cluster,
    source_trust,
    titles_are_similar,
)


class TestSourceTrust:
    def test_known_outlet_scores_high(self):
        assert source_trust("Reuters") >= 0.95
        assert source_trust("BBC News") >= 0.9

    def test_unknown_outlet_gets_default(self):
        assert source_trust("Random Blog XYZ") == 0.5

    def test_empty_name_gets_default(self):
        assert source_trust("") == 0.5


class TestTitleSimilarity:
    def test_exact_match(self):
        assert titles_are_similar("Major policy shift announced", "Major policy shift announced")

    def test_near_duplicate(self):
        assert titles_are_similar(
            "Fed raises interest rates by 25 basis points",
            "Fed raises interest rates 25 basis points",
        )

    def test_unrelated(self):
        assert not titles_are_similar(
            "Local team wins championship",
            "Scientists discover new exoplanet",
        )


class TestClustering:
    def test_clusters_same_url(self):
        articles = [
            {
                "title": "A",
                "url": "https://example.com/story?utm_source=x",
                "source": {"name": "BBC"},
            },
            {
                "title": "B",
                "url": "https://www.example.com/story",
                "source": {"name": "Reuters"},
            },
        ]
        clusters = cluster_articles(articles)
        assert len(clusters) == 1
        assert len(clusters[0]) == 2

    def test_clusters_similar_titles(self):
        articles = [
            {
                "title": "Earthquake strikes coastal region killing dozens",
                "url": "https://a.com/1",
                "source": {"name": "AP"},
            },
            {
                "title": "Earthquake strikes coastal region killing dozens more",
                "url": "https://b.com/2",
                "source": {"name": "Reuters"},
            },
        ]
        clusters = cluster_articles(articles)
        assert len(clusters) == 1

    def test_pick_representative_prefers_trusted_source(self):
        cluster = [
            {
                "title": "Story",
                "url": "https://blog.example/1",
                "publishedAt": "2025-01-02T00:00:00+00:00",
                "source": {"name": "Random Blog"},
            },
            {
                "title": "Story",
                "url": "https://reuters.com/1",
                "publishedAt": "2025-01-01T00:00:00+00:00",
                "source": {"name": "Reuters"},
            },
        ]
        rep = pick_representative(cluster)
        assert rep["source"]["name"] == "Reuters"
        assert rep["clusterSize"] == 2
        assert "Random Blog" in rep["relatedSources"]


class TestRankAndCluster:
    def test_ranks_corroborated_trusted_story_first(self):
        now = datetime(2025, 1, 3, tzinfo=UTC)
        api = [
            {
                "title": "Central bank hikes rates amid inflation fight",
                "url": "https://reuters.com/rates",
                "publishedAt": "2025-01-02T12:00:00+00:00",
                "source": {"name": "Reuters"},
            },
            {
                "title": "Celebrity wears hat at awards show",
                "url": "https://blog.example/hat",
                "publishedAt": "2025-01-03T00:00:00+00:00",
                "source": {"name": "Gossip Blog"},
            },
        ]
        rss = [
            {
                "title": "Central bank hikes rates amid inflation fight worldwide",
                "url": "https://bbc.com/rates",
                "publishedAt": "2025-01-02T11:00:00+00:00",
                "source": {"name": "BBC"},
            },
        ]

        ranked = rank_and_cluster(api, rss, limit=10, now=now)

        assert len(ranked) == 2
        assert "rates" in ranked[0]["url"]
        assert ranked[0]["clusterSize"] == 2
        assert ranked[0]["relatedSources"]
