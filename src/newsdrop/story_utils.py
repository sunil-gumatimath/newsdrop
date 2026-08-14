"""Shared utilities for title similarity, URL canonicalization, and deduplication.

Extracted from ``story_ranker.py`` to keep title/URL matching logic in one
place for all merge paths.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

from .config import WORD_RE

Article = dict[str, Any]

# Default Jaccard threshold for title-based dedup (used by story_ranker).
_TITLE_SIMILARITY_THRESHOLD = 0.62
_MIN_TITLE_CHARS = 20


def normalize_title(title: str) -> str:
    """Lowercase, tokenize via WORD_RE, and rejoin."""
    if not title:
        return ""
    return " ".join(WORD_RE.findall(title.lower()))


def title_similarity(left: str, right: str) -> float:
    """Jaccard similarity of normalized word sets."""
    left_words = set(normalize_title(left).split())
    right_words = set(normalize_title(right).split())
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


def titles_are_similar(
    left: str,
    right: str,
    *,
    threshold: float = _TITLE_SIMILARITY_THRESHOLD,
    min_chars: int = _MIN_TITLE_CHARS,
) -> bool:
    """True when two headlines likely describe the same story.

    ``threshold`` and ``min_chars`` can be overridden by callers that
    need stricter or more relaxed matching.
    """
    left_norm = normalize_title(left)
    right_norm = normalize_title(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    if len(left_norm) < min_chars and len(right_norm) < min_chars:
        return left_norm == right_norm
    if (
        len(left_norm) >= min_chars
        and len(right_norm) >= min_chars
        and (left_norm in right_norm or right_norm in left_norm)
    ):
        return True
    return title_similarity(left, right) >= threshold


def canonical_url(url: str) -> str:
    """Normalize a URL for dedupe / cross-source matching.

    Strips www prefix, trailing slash, and tracking params (utm_*, ref,
    fbclid, gclid).
    """
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
        f"{key}={value}" for key in sorted(filtered) for value in sorted(filtered[key])
    )

    return urlunparse((parsed.scheme.lower(), host, path, "", query, ""))


def same_story(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """True when two article dicts describe the same story (URL or title match)."""
    left_url = canonical_url(str(left.get("url", "") or ""))
    right_url = canonical_url(str(right.get("url", "") or ""))
    if left_url and right_url and left_url == right_url:
        return True
    return titles_are_similar(
        str(left.get("title", "")),
        str(right.get("title", "")),
    )


def source_name(article: dict[str, Any]) -> str:
    """Extract the display name from an article's ``source`` field."""
    source_obj = article.get("source", {})
    if isinstance(source_obj, dict):
        return str(source_obj.get("name", "") or "")
    return str(source_obj or "")
