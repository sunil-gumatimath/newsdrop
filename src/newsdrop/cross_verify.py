"""Cross-source popularity signal between news headlines and Reddit posts.

Marks articles that appear in both traditional news sources (API/RSS) and
Reddit discussion. Reddit is a **trending / popularity** signal, not a
fact-check or verification source.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

from .config import WORD_RE

Article = dict[str, Any]

# Minimum Jaccard word overlap for title-only matches (no shared URL).
_TITLE_SIMILARITY_THRESHOLD = 0.70
# Title-only matches require a reasonably long normalized title.
_MIN_TITLE_CHARS = 24
# Skip weak Reddit posts when matching on title alone (URL matches always ok).
_MIN_REDDIT_SCORE_FOR_TITLE_MATCH = 10


def _normalize_title(title: str) -> str:
    if not title:
        return ""
    return " ".join(WORD_RE.findall(title.lower()))


def canonical_url(url: str) -> str:
    """Normalize a URL for dedupe / cross-source matching."""
    candidate = url.strip()
    if not candidate:
        return ""

    try:
        parsed = urlparse(candidate)
    except Exception:
        return ""

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    path = parsed.path.rstrip("/") or "/"
    query_pairs = parse_qs(parsed.query, keep_blank_values=False)
    filtered = {
        key: values
        for key, values in query_pairs.items()
        if not key.lower().startswith("utm_") and key.lower() not in {"ref", "fbclid", "gclid"}
    }
    query = "&".join(
        f"{key}={value}"
        for key in sorted(filtered)
        for value in sorted(filtered[key])
    )

    return urlunparse((parsed.scheme.lower(), host, path, "", query, ""))


def _title_similarity(left: str, right: str) -> float:
    left_words = set(_normalize_title(left).split())
    right_words = set(_normalize_title(right).split())
    if not left_words or not right_words:
        return 0.0
    intersection = left_words & right_words
    union = left_words | right_words
    return len(intersection) / len(union)


def _titles_match(left: str, right: str) -> bool:
    left_norm = _normalize_title(left)
    right_norm = _normalize_title(right)
    if not left_norm or not right_norm:
        return False
    if len(left_norm) < _MIN_TITLE_CHARS and len(right_norm) < _MIN_TITLE_CHARS:
        return left_norm == right_norm
    if left_norm == right_norm:
        return True
    if len(left_norm) >= _MIN_TITLE_CHARS and len(right_norm) >= _MIN_TITLE_CHARS:
        if left_norm in right_norm or right_norm in left_norm:
            return True
    return _title_similarity(left, right) >= _TITLE_SIMILARITY_THRESHOLD


def _find_reddit_match(article: Article, reddit_posts: list[Article]) -> Article | None:
    article_url = canonical_url(str(article.get("url", "")))

    if article_url:
        for post in reddit_posts:
            post_url = canonical_url(str(post.get("url", "")))
            if post_url and post_url == article_url:
                return post

    article_title = str(article.get("title", ""))
    best: Article | None = None
    best_score = 0.0
    for post in reddit_posts:
        post_title = str(post.get("title", ""))
        if not _titles_match(article_title, post_title):
            continue
        try:
            reddit_score = int(post.get("redditScore", 0) or 0)
        except (TypeError, ValueError):
            reddit_score = 0
        if reddit_score < _MIN_REDDIT_SCORE_FOR_TITLE_MATCH:
            continue
        score = _title_similarity(article_title, post_title)
        if score > best_score:
            best_score = score
            best = post
    return best


def apply_cross_verification(
    articles: list[Article],
    reddit_posts: list[Article],
) -> list[Article]:
    """Annotate news articles with Reddit popularity signal and rank them higher.

    Sets ``redditTrending`` (preferred) and keeps ``crossConfirmed`` as a
    backward-compatible alias with the same boolean meaning: "also discussed
    on Reddit", not "fact-checked".
    """
    if not articles or not reddit_posts:
        return articles

    trending: list[Article] = []
    other: list[Article] = []

    for article in articles:
        tagged = dict(article)
        match = _find_reddit_match(tagged, reddit_posts)
        if match is not None:
            tagged["redditTrending"] = True
            tagged["crossConfirmed"] = True  # legacy alias — not a truth claim
            tagged["redditSubreddit"] = match.get("redditSubreddit", "")
            tagged["redditScore"] = match.get("redditScore", 0)
            tagged["redditPermalink"] = match.get("redditPermalink", "")
            trending.append(tagged)
        else:
            tagged["redditTrending"] = False
            tagged["crossConfirmed"] = False
            other.append(tagged)

    # Prefer higher Reddit score among trending matches, then recency.
    trending.sort(
        key=lambda a: (
            int(a.get("redditScore", 0) or 0),
            str(a.get("publishedAt", "") or ""),
        ),
        reverse=True,
    )
    other.sort(key=lambda a: str(a.get("publishedAt", "") or ""), reverse=True)
    return trending + other
