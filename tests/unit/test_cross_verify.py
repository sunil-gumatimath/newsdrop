"""Tests for cross_verify.py — URL/title matching and ranking."""

from __future__ import annotations

from newsdrop.cross_verify import (
    apply_cross_verification,
    canonical_url,
)


class TestCanonicalUrl:
    def test_strips_www_and_trailing_slash(self):
        assert canonical_url("https://www.example.com/path/") == "https://example.com/path"

    def test_strips_utm_params(self):
        left = canonical_url("https://example.com/a?utm_source=twitter&id=1")
        right = canonical_url("https://example.com/a?id=1")
        assert left == right

    def test_rejects_non_http(self):
        assert canonical_url("javascript:alert(1)") == ""


class TestApplyCrossVerification:
    def test_url_match_marks_cross_confirmed(self):
        news = [
            {
                "title": "Major policy shift announced",
                "url": "https://example.com/story?utm_source=x",
                "publishedAt": "2025-01-02T00:00:00+00:00",
            }
        ]
        reddit = [
            {
                "title": "Major policy shift announced",
                "url": "https://example.com/story",
                "redditSubreddit": "worldnews",
                "redditScore": 500,
                "redditPermalink": "https://www.reddit.com/r/worldnews/comments/abc",
            }
        ]

        result = apply_cross_verification(news, reddit)

        assert len(result) == 1
        assert result[0]["crossConfirmed"] is True
        assert result[0]["redditSubreddit"] == "worldnews"
        assert result[0]["redditScore"] == 500

    def test_title_match_when_urls_differ(self):
        news = [
            {
                "title": "NASA launches new Mars rover mission today",
                "url": "https://nasa.gov/mars-rover",
                "publishedAt": "2025-01-03T00:00:00+00:00",
            }
        ]
        reddit = [
            {
                "title": "NASA launches new Mars rover mission today - discussion",
                "url": "",
                "redditSubreddit": "space",
                "redditScore": 120,
                "redditPermalink": "https://www.reddit.com/r/space/comments/xyz",
            }
        ]

        result = apply_cross_verification(news, reddit)

        assert result[0]["crossConfirmed"] is True
        assert result[0]["redditTrending"] is True
        assert result[0]["redditSubreddit"] == "space"

    def test_title_match_requires_min_reddit_score(self):
        news = [
            {
                "title": "NASA launches new Mars rover mission today",
                "url": "https://nasa.gov/mars-rover",
                "publishedAt": "2025-01-03T00:00:00+00:00",
            }
        ]
        reddit = [
            {
                "title": "NASA launches new Mars rover mission today - discussion",
                "url": "",
                "redditSubreddit": "space",
                "redditScore": 2,
                "redditPermalink": "https://www.reddit.com/r/space/comments/xyz",
            }
        ]
        result = apply_cross_verification(news, reddit)
        assert result[0]["crossConfirmed"] is False

    def test_verified_articles_rank_first(self):
        news = [
            {
                "title": "Unverified older story",
                "url": "https://example.com/old",
                "publishedAt": "2025-01-05T00:00:00+00:00",
            },
            {
                "title": "Verified newer story",
                "url": "https://example.com/new",
                "publishedAt": "2025-01-01T00:00:00+00:00",
            },
        ]
        reddit = [
            {
                "title": "Verified newer story",
                "url": "https://example.com/new",
                "redditSubreddit": "news",
                "redditScore": 10,
                "redditPermalink": "https://www.reddit.com/r/news/comments/1",
            }
        ]

        result = apply_cross_verification(news, reddit)

        assert result[0]["title"] == "Verified newer story"
        assert result[0]["crossConfirmed"] is True
        assert result[1]["crossConfirmed"] is False

    def test_no_reddit_posts_returns_unchanged(self):
        news = [{"title": "Solo headline", "url": "https://example.com/a", "publishedAt": ""}]
        assert apply_cross_verification(news, []) == news

    def test_no_false_positive_on_unrelated_titles(self):
        news = [{"title": "Local school board election results", "url": "https://example.com/a"}]
        reddit = [{"title": "Best gaming laptop 2025", "url": "https://example.com/b"}]
        result = apply_cross_verification(news, reddit)
        assert result[0]["crossConfirmed"] is False
