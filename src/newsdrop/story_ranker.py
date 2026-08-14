"""Story clustering, source trust weights, and ranking for digests.

Turns a multi-source article pile into a short, high-signal list:
1. Cluster near-duplicate stories (same URL or similar titles)
2. Pick the best representative per cluster (trusted source + recency)
3. Rank clusters by corroboration, source quality, and freshness

Title-similarity and URL-canonicalisation logic is imported from
:mod:`newsdrop.story_utils`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .story_utils import (
    same_story,
    source_name,
    titles_are_similar,
)

# Re-exported for backward compatibility with callers/tests that import from
# ``newsdrop.story_ranker``.
__all__ = ["same_story", "source_name", "titles_are_similar"]

Article = dict[str, Any]

# Higher = preferred when picking a cluster representative / ranking.
# Matched case-insensitively as substring against source display name.
SOURCE_TRUST: dict[str, float] = {
    "reuters": 1.0,
    "associated press": 1.0,
    "ap news": 1.0,
    "bbc": 0.95,
    "the guardian": 0.9,
    "guardian": 0.9,
    "new york times": 0.9,
    "nytimes": 0.9,
    "nyt": 0.9,
    "washington post": 0.88,
    "financial times": 0.88,
    "bloomberg": 0.88,
    "wall street journal": 0.88,
    "wsj": 0.88,
    "npr": 0.85,
    "al jazeera": 0.82,
    "the hindu": 0.8,
    "times of india": 0.78,
    "ndtv": 0.78,
    "indian express": 0.78,
    "hindustan times": 0.76,
    "sky news": 0.8,
    "abc news": 0.8,
    "deutsche welle": 0.8,
    "france 24": 0.8,
    "the verge": 0.75,
    "wired": 0.75,
    "ars technica": 0.75,
    "techcrunch": 0.72,
    "cnbc": 0.78,
    "espn": 0.75,
    "variety": 0.7,
    "factcheck.org": 0.85,
    "nasa": 0.85,
}

_DEFAULT_TRUST = 0.5
_TITLE_SIMILARITY_THRESHOLD = 0.62
_MIN_TITLE_CHARS = 20

# Ranking weights (tunable).
_W_TRUST = 3.0
_W_CLUSTER = 1.5  # corroboration across outlets
_W_RECENCY = 2.0  # 0..1 freshness score


def source_trust(source_name_str: str) -> float:
    """Return a trust score in ``[0, 1]`` for a source display name."""
    name = (source_name_str or "").strip().lower()
    if not name:
        return _DEFAULT_TRUST

    best = _DEFAULT_TRUST
    for key, weight in SOURCE_TRUST.items():
        if key in name:
            best = max(best, weight)
    return best


def _parse_published(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        # Support trailing Z and bare ISO strings.
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _recency_score(article: Article, now: datetime | None = None) -> float:
    """Map publish time to 0..1 (fresher → higher). Unknown dates score mid-low."""
    published = _parse_published(article.get("publishedAt"))
    if published is None:
        return 0.25
    now = now or datetime.now(tz=UTC)
    age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
    # Full score within 6h, ~0.5 at 24h, near-zero after ~3 days.
    return max(0.0, 1.0 - (age_hours / 72.0))


def cluster_articles(articles: list[Article]) -> list[list[Article]]:
    """Greedy cluster of near-duplicate articles (order-preserving)."""
    clusters: list[list[Article]] = []
    for article in articles:
        placed = False
        for cluster in clusters:
            if same_story(cluster[0], article):
                cluster.append(article)
                placed = True
                break
        if not placed:
            clusters.append([article])
    return clusters


def _has_usable_blurb(article: Article) -> float:
    """Prefer representatives that include a real description/content snippet."""
    for key in ("description", "content"):
        text = str(article.get(key, "") or "").strip()
        if len(text) >= 40:
            return 1.0
    return 0.0


def _article_rank_key(
    article: Article, now: datetime | None = None
) -> tuple[float, float, float, str]:
    trust = source_trust(source_name(article))
    recency = _recency_score(article, now=now)
    blurb = _has_usable_blurb(article)
    published = str(article.get("publishedAt", "") or "")
    return (trust, blurb, recency, published)


def pick_representative(cluster: list[Article], now: datetime | None = None) -> Article:
    """Choose the best article in a cluster and annotate corroboration metadata."""
    if len(cluster) == 1:
        rep = dict(cluster[0])
        rep.setdefault("clusterSize", 1)
        rep.setdefault("relatedSources", [])
        return rep

    ranked = sorted(cluster, key=lambda a: _article_rank_key(a, now=now), reverse=True)
    rep = dict(ranked[0])
    names: list[str] = []
    seen: set[str] = set()
    for article in ranked:
        name = source_name(article).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)

    primary = source_name(rep).strip()
    related = [n for n in names if n.lower() != primary.lower()]
    rep["clusterSize"] = len(cluster)
    rep["relatedSources"] = related[:5]
    return rep


def _safe_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _cluster_score(rep: Article, now: datetime | None = None) -> float:
    trust = source_trust(source_name(rep))
    cluster_size = _safe_int(rep.get("clusterSize"), 1)
    # Diminishing returns after ~4 outlets.
    cluster_signal = min(cluster_size - 1, 3) / 3.0
    recency = _recency_score(rep, now=now)

    return _W_TRUST * trust + _W_CLUSTER * cluster_signal + _W_RECENCY * recency


def rank_and_cluster(
    *sources: list[Article],
    limit: int = 10,
    now: datetime | None = None,
) -> list[Article]:
    """Merge sources, cluster duplicates, rank, and return top ``limit`` stories."""
    flat: list[Article] = []
    for source in sources:
        flat.extend(source)

    if not flat:
        return []

    clusters = cluster_articles(flat)
    representatives = [pick_representative(cluster, now=now) for cluster in clusters]
    representatives.sort(
        key=lambda a: (
            _cluster_score(a, now=now),
            str(a.get("publishedAt", "") or ""),
        ),
        reverse=True,
    )
    return representatives[:limit]
