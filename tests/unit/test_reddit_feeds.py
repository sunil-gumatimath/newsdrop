"""Tests for reddit_feeds.py — subreddit selection and post normalisation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from newsdrop import reddit_feeds


class TestSubredditSelection:
    def test_country_and_category_subreddits_are_merged(self):
        subs = reddit_feeds._subreddits_for("us", "technology")
        assert "news" in subs
        assert "technology" in subs

    def test_has_reddit_for_known_country(self):
        assert reddit_feeds.has_reddit_for("us", "general") is True

    def test_has_reddit_for_unknown_country_uses_category_fallback(self):
        assert reddit_feeds.has_reddit_for("zz", "science") is True


class TestPostToArticle:
    def test_external_link_post(self):
        post = {
            "title": "Headline",
            "url": "https://example.com/article",
            "permalink": "/r/worldnews/comments/abc/headline/",
            "subreddit": "worldnews",
            "score": 842,
            "created_utc": 1_700_000_000,
            "is_self": False,
        }
        article = reddit_feeds._post_to_article(post, "worldnews")
        assert article["url"] == "https://example.com/article"
        assert article["redditSubreddit"] == "worldnews"
        assert article["redditScore"] == 842
        assert article["source"] == {"name": "r/worldnews"}

    def test_self_post_has_empty_external_url(self):
        post = {
            "title": "Discussion thread",
            "url": "https://www.reddit.com/r/news/comments/abc/discussion/",
            "permalink": "/r/news/comments/abc/discussion/",
            "subreddit": "news",
            "score": 10,
            "created_utc": 1_700_000_000,
            "is_self": True,
        }
        article = reddit_feeds._post_to_article(post, "news")
        assert article["url"] == ""
        assert article["redditIsSelf"] is True


@pytest.mark.asyncio
async def test_fetch_reddit_posts_parses_json_response():
    payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "Breaking story",
                        "url": "https://example.com/breaking",
                        "permalink": "/r/news/comments/xyz/breaking/",
                        "subreddit": "news",
                        "score": 200,
                        "created_utc": 1_700_000_000,
                        "is_self": False,
                        "stickied": False,
                    }
                }
            ]
        }
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"data":{"children":[]}}'
    mock_response.json.return_value = payload

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("newsdrop.reddit_feeds.httpx.AsyncClient", return_value=mock_client):
        with patch("newsdrop.reddit_feeds._subreddits_for", return_value=["news"]):
            articles = await reddit_feeds.fetch_reddit_posts("us", "general")

    assert len(articles) == 1
    assert articles[0]["title"] == "Breaking story"
    assert articles[0]["url"] == "https://example.com/breaking"


@pytest.mark.asyncio
async def test_fetch_reddit_posts_falls_back_to_rss_when_json_is_blocked():
    rss_body = b"""
    <?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>RSS fallback story</title>
        <link href="https://www.reddit.com/r/news/comments/abc/story/" />
        <published>2025-01-01T12:00:00+00:00</published>
      </entry>
    </feed>
    """

    json_response = MagicMock()
    json_response.status_code = 403
    json_response.content = b"blocked"

    rss_response = MagicMock()
    rss_response.status_code = 200
    rss_response.content = rss_body

    mock_client = AsyncMock()
    mock_client.get.side_effect = [json_response, rss_response]
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("newsdrop.reddit_feeds.httpx.AsyncClient", return_value=mock_client):
        with patch("newsdrop.reddit_feeds._subreddits_for", return_value=["news"]):
            articles = await reddit_feeds.fetch_reddit_posts("us", "general")

    assert len(articles) == 1
    assert articles[0]["title"] == "RSS fallback story"
    assert articles[0]["url"] == ""
    assert articles[0]["redditPermalink"] == "https://www.reddit.com/r/news/comments/abc/story/"
