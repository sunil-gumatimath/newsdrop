from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import urlparse

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError

from ..config import (
    ADMIN_CHAT_IDS,
    CATEGORIES,
    COUNTRIES,
)
from ..config import (
    NEWS_COOLDOWN_SECONDS as _NEWS_COOLDOWN_SECONDS,
)
from ..config import (
    SEARCH_COOLDOWN_SECONDS as _SEARCH_COOLDOWN_SECONDS,
)
from ..database import (
    is_following_topic,
)
from ..news_fetcher import (
    fetch_trending_topics,
)

logger = logging.getLogger(__name__)

Article = dict[str, Any]
NewsResponse = dict[str, Any]
Prefs = dict[str, str]


class DigestResult(NamedTuple):
    digest: str | None
    empty_message: str | None
    reply_markup: InlineKeyboardMarkup | None = None


# Per-user cooldown scopes. Backed by ``newsdrop.state`` (Redis when
# ``REDIS_URL`` is set, in-memory otherwise). The cooldown window length is
# tunable via the ``NEWS_COOLDOWN_SECONDS`` / ``SEARCH_COOLDOWN_SECONDS`` env
# vars (see config.py); 0 disables the cooldown entirely.
SEARCH_RATE_LIMIT_SCOPE = "search"
SEARCH_COOLDOWN_SECONDS = _SEARCH_COOLDOWN_SECONDS

NEWS_RATE_LIMIT_SCOPE = "news"
NEWS_COOLDOWN_SECONDS = _NEWS_COOLDOWN_SECONDS

TRENDING_RATE_LIMIT_SCOPE = "trending"
TRENDING_COOLDOWN_SECONDS = _NEWS_COOLDOWN_SECONDS

MAX_FOLLOW_TOPIC_LENGTH = 40

TRENDING_CATEGORY_ALIASES = {
    "general": "general",
    "top": "general",
    "all": "general",
    "tech": "technology",
    "technology": "technology",
    "biz": "business",
    "business": "business",
    "sport": "sports",
    "sports": "sports",
    "ent": "entertainment",
    "entertainment": "entertainment",
    "health": "health",
    "sci": "science",
    "science": "science",
}


def _escape_html(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _truncate_text(value: object, max_length: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_length:
        return text
    # Prefer cutting on a word boundary so Telegram blurbs don't look broken.
    cut = text[: max_length - 3].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "..."


def _clean_blurb_text(value: object) -> str:
    """Strip HTML/boilerplate from API/RSS description or content fields."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""

    # HTML → plain text (RSS/API sometimes ship markup).
    text = re.sub(r"<br\s*/?>|</p>|</div>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)

    # Common feed/API noise.
    text = re.sub(r"\[\s*\+?\d+\s*chars?\s*\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\s*\.\.\.\s*\]", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\n\r-–—|·•")

    # Drop useless stubs.
    low = text.lower()
    if low in {"", "null", "none", "n/a", "na", "undefined"}:
        return ""
    if low.startswith("click here") or low.startswith("read more"):
        return ""
    return text


def _article_blurb(article: Article, max_length: int = 140) -> str:
    """Best short description available for an article.

    Prefers ``description``, then ``content``. Always returns a plain string
    suitable for Telegram italics (caller still HTML-escapes).
    """
    candidates = (
        article.get("description"),
        article.get("content"),
        article.get("summary"),
    )
    title = _clean_blurb_text(article.get("title", "")).lower()

    for raw in candidates:
        text = _clean_blurb_text(raw)
        if not text:
            continue
        # Skip blurbs that are just the title repeated.
        if title and text.lower() == title:
            continue
        if title and text.lower().startswith(title) and len(text) - len(title) < 12:
            continue
        if len(text) < 20:
            # Too short to be useful as a description.
            continue
        return _truncate_text(text, max_length)

    return ""


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


def _normalize_topic(topic: str) -> str:
    return " ".join(topic.strip().split())


def _format_relative_time(iso_timestamp: str) -> str:
    """Return a humanized 'time ago' string for a Telegram message.

    Examples: "just now", "5m ago", "2h ago", "3d ago", "2024-11-30" (>= 7d).
    Returns empty string if the timestamp is unparseable.
    """
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 0:
            return "just now"
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        if days < 7:
            return f"{days}d ago"
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _sanitize_follow_topic(topic: str) -> str:
    normalized = _normalize_topic(topic)
    if len(normalized) > MAX_FOLLOW_TOPIC_LENGTH:
        normalized = normalized[:MAX_FOLLOW_TOPIC_LENGTH].rstrip()
    return normalized


def _resolve_trending_category(raw_value: str | None) -> str | None:
    if raw_value is None:
        return "general"

    normalized = raw_value.strip().lower()
    if not normalized:
        return "general"

    return TRENDING_CATEGORY_ALIASES.get(normalized)


def _category_label(category: str) -> str:
    return "Top" if category == "general" else category.capitalize()


def _country_display(country: str) -> str:
    """Human label for a country code, e.g. ``us`` → ``🇺🇸 US``."""
    code = (country or "").strip().lower()
    if not code:
        return "—"
    for label, value in COUNTRIES.items():
        if value == code:
            # Labels look like "🇺🇸 United States" — keep flag + short code.
            flag = label.split(" ", 1)[0] if label else ""
            if flag and not flag.isascii():
                return f"{flag} {code.upper()}"
            return code.upper()
    return code.upper()


def _format_source_line(article: Article) -> str:
    """Build a clean meta line: time · source · also covered by …"""
    _, source_escaped = _get_source_name(article)
    rel_time = _format_relative_time(str(article.get("publishedAt", "")))

    parts: list[str] = []
    if rel_time:
        parts.append(f"⏱ {rel_time}")
    parts.append(f"📍 {source_escaped}")

    related = article.get("relatedSources") or []
    if isinstance(related, list) and related:
        also = " · ".join(_escape_html(str(s)) for s in related[:3] if s)
        if also:
            parts.append(f"also {also}")
    else:
        try:
            cluster_size = int(article.get("clusterSize", 1) or 1)
        except (TypeError, ValueError):
            cluster_size = 1
        if cluster_size > 1:
            parts.append(f"{cluster_size} sources")

    return "  ·  ".join(parts)


def _country_name_from_code(code: str) -> str:
    return next((name for name, value in COUNTRIES.items() if value == code), code)


def _effective_chat_id(update: Update) -> int | None:
    chat = update.effective_chat
    return chat.id if chat else None


def is_admin_chat(chat_id: int | None) -> bool:
    """Return True if chat_id is listed in ADMIN_CHAT_IDS (ops commands)."""
    if chat_id is None or not ADMIN_CHAT_IDS:
        return False
    return chat_id in ADMIN_CHAT_IDS


def _get_articles(payload: dict[str, Any]) -> list[Article]:
    articles = payload.get("articles", [])
    return articles if isinstance(articles, list) else []


def _match_followed_topics(article: Article, followed_topics: list[str] | None) -> list[str]:
    """Return followed topics that appear in this article (word-boundary match)."""
    if not followed_topics:
        return []
    blob = (
        f"{article.get('title', '')} {article.get('description', '')} {article.get('content', '')}"
    )
    hits: list[str] = []
    for topic in followed_topics:
        q = topic.lower().strip()
        if not q:
            continue
        pattern = re.compile(rf"\b{re.escape(q)}\b", re.IGNORECASE)
        if pattern.search(blob) or q in blob.lower():
            hits.append(topic)
    return hits


def _personalize_articles(
    articles: list[Article],
    followed_topics: list[str] | None = None,
) -> list[Article]:
    """Tag why-this-story reasons and put followed matches first."""
    from ..story_ranker import source_trust

    enriched: list[Article] = []
    for article in articles:
        item = dict(article)
        reasons: list[str] = []
        matched = _match_followed_topics(item, followed_topics)
        if matched:
            item["matchedTopics"] = matched
            reasons.append(f"📌 #{matched[0]}")
        try:
            cluster_size = int(item.get("clusterSize", 1) or 1)
        except (TypeError, ValueError):
            cluster_size = 1
        related = item.get("relatedSources") or []
        if cluster_size > 1 or (isinstance(related, list) and related):
            reasons.append("🗞 Multi-source")
        source_obj = item.get("source", {})
        source_name = (
            str(source_obj.get("name", ""))
            if isinstance(source_obj, dict)
            else str(source_obj or "")
        )
        if source_trust(source_name) >= 0.9:
            reasons.append("⭐ Trusted")
        item["whyTags"] = reasons
        enriched.append(item)

    def _sort_key(a: Article) -> tuple[int, int, str]:
        matched = a.get("matchedTopics") or []
        has_follow = 1 if matched else 0
        cluster = 0
        try:
            cluster_size = int(a.get("clusterSize", 1) or 1)
            cluster = min(cluster_size, 5)
        except (TypeError, ValueError):
            pass
        return (has_follow, cluster, str(a.get("publishedAt", "") or ""))

    enriched.sort(key=_sort_key, reverse=True)
    return enriched


def _build_digest_keyboard(
    articles: list[Article],
    *,
    follow_topic: str | None = None,
) -> InlineKeyboardMarkup | None:
    """URL buttons for the top stories (2 per row, max 8 stories).

    Optionally append a Follow row for search results.
    """
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, article in enumerate(articles[:8], 1):
        url = _safe_url(article.get("url", ""))
        if not url:
            continue
        raw_source, _ = _get_source_name(article)
        short = (raw_source or "Read")[:14]
        label = f"{i} · {short}"
        row.append(InlineKeyboardButton(label, url=url))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    topic = _sanitize_follow_topic(follow_topic or "")
    if topic:
        cb = f"follow:{topic}"
        if len(cb.encode("utf-8")) <= 64:
            rows.append([InlineKeyboardButton(f"➕ Follow #{topic}", callback_data=cb)])

    return InlineKeyboardMarkup(rows) if rows else None


def build_search_payload(data: NewsResponse, query: str) -> DigestResult:
    """Format search results like the /news digest, with open + follow buttons."""
    articles = _get_articles(data)
    q = (query or "").strip()
    if not articles:
        empty = (
            f"🔍 <b>No results for “{_escape_html(q)}”</b>\n\n"
            "We only show whole-word matches (so “AI” won’t match “airport”).\n"
            "Try another keyword, or /news for your usual briefing."
        )
        return DigestResult(None, empty, _empty_digest_keyboard())

    shown = articles[:8]
    divider = "─" * 18
    lines: list[str] = [
        f"🔍 <b>Search: “{_escape_html(q)}”</b>",
        f"{len(shown)} result{'s' if len(shown) != 1 else ''}",
        divider,
        "",
    ]

    for i, article in enumerate(shown, 1):
        title = _escape_html(article.get("title", "No title"))
        url = _safe_url(article.get("url", ""))
        blurb = _article_blurb(article, max_length=130)

        if url:
            escaped_url = html.escape(url, quote=True)
            lines.append(f'<b>{i}.</b> <a href="{escaped_url}"><b>{title}</b></a>')
        else:
            lines.append(f"<b>{i}. {title}</b>")

        if blurb:
            lines.append(f"<i>{_escape_html(blurb)}</i>")
        lines.append(_format_source_line(article))
        lines.append("")

    lines.append(divider)
    total = data.get("totalResults", len(shown)) if isinstance(data, dict) else len(shown)
    lines.append(f"💡 Showing top {len(shown)} of {total}  ·  buttons open full articles")
    digest = "\n".join(lines)
    return DigestResult(digest, None, _build_digest_keyboard(shown, follow_topic=q))


def format_breaking_alert(
    article: Article,
    matched_keywords: list[str],
    *,
    used_today: int,
    max_per_day: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """One compact breaking-alert message + optional open button."""
    title = _escape_html(article.get("title", "No title"))
    url = _safe_url(article.get("url", ""))
    blurb = _article_blurb(article, max_length=160)
    _, source = _get_source_name(article)
    rel = _format_relative_time(str(article.get("publishedAt", "")))

    # Prefer the strongest signal: keywords that hit the title.
    title_l = str(article.get("title", "")).lower()
    title_hits = [k for k in matched_keywords if re.search(rf"\b{re.escape(k.lower())}\b", title_l)]
    shown_kw = title_hits or matched_keywords
    kw_label = ", ".join(f"#{_escape_html(k)}" for k in shown_kw[:3])

    lines = [
        "🚨 <b>Breaking</b>",
        f"Matched {kw_label}" if kw_label else "Matched your alert keywords",
        "",
    ]
    if url:
        escaped_url = html.escape(url, quote=True)
        lines.append(f'<a href="{escaped_url}"><b>{title}</b></a>')
    else:
        lines.append(f"<b>{title}</b>")
    if blurb:
        lines.append(f"<i>{_escape_html(blurb)}</i>")

    meta: list[str] = []
    if rel:
        meta.append(f"⏱ {rel}")
    meta.append(f"📍 {source}")
    lines.append("  ·  ".join(meta))

    remaining = max(0, max_per_day - used_today)
    lines.append("")
    lines.append(f"🔔 Alert {used_today}/{max_per_day} today  ·  {remaining} left")

    keyboard = None
    if url:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📖 Read full story", url=url)]])
    return "\n".join(lines), keyboard


def _empty_digest_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🌍 Region", callback_data="menu:country"),
                InlineKeyboardButton("📂 Category", callback_data="menu:category"),
            ],
            [InlineKeyboardButton("🔍 Search instead", callback_data="menu:search_hint")],
        ]
    )


def country_keyboard(onboarding: bool = False) -> InlineKeyboardMarkup:
    prefix = "obcountry" if onboarding else "country"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(display, callback_data=f"{prefix}:{code}")]
            for display, code in COUNTRIES.items()
        ]
    )


def category_keyboard(onboarding: bool = False) -> InlineKeyboardMarkup:
    prefix = "obcategory" if onboarding else "category"
    # Two columns for a denser mobile layout.
    buttons = [
        InlineKeyboardButton(cat.capitalize(), callback_data=f"{prefix}:{cat}")
        for cat in CATEGORIES
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    return InlineKeyboardMarkup(rows)


def onboarding_finish_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Subscribe to daily news", callback_data="obsub:1")],
            [
                InlineKeyboardButton("📰 Get news now", callback_data="obnews:1"),
                InlineKeyboardButton("Skip", callback_data="obsub:0"),
            ],
        ]
    )


def _build_digest_payload(
    data: NewsResponse,
    category: str,
    country: str,
    followed_topics: list[str] | None = None,
) -> DigestResult:
    """Return either a formatted HTML digest or an empty-state message.

    Returns ``DigestResult(digest, None, keyboard)`` when articles are available and
    ``DigestResult(None, empty_message, empty_keyboard)`` otherwise. Centralizes the
    no-results copy shared by ``/news`` and the scheduled daily job.
    """
    articles = _get_articles(data)
    if articles:
        sources = data.get("sources", []) if isinstance(data, dict) else []
        personalized = _personalize_articles(articles, followed_topics)
        shown = personalized[:10]
        digest = _format_news_digest(shown, category, country, sources)
        return DigestResult(digest, None, _build_digest_keyboard(shown))

    sources_used = data.get("sources", []) if isinstance(data, dict) else []
    region = _escape_html(_country_display(country))
    cat = _escape_html(_category_label(category))
    if sources_used:
        hint = (
            "Nothing matched this combo right now.\n"
            "Try another <b>region</b> or <b>category</b> below, then /news again."
        )
    else:
        hint = (
            "The news service may be temporarily unavailable.\n"
            "Try again in a minute, or use /search &lt;topic&gt;."
        )

    return DigestResult(
        None,
        f"📭 <b>No stories found</b>\n{cat}  ·  {region}\n\n{hint}",
        _empty_digest_keyboard(),
    )


def _get_article_key(article: Article) -> str:
    url = _safe_url(article.get("url", ""))
    if url:
        return url

    title = _normalize_topic(str(article.get("title", ""))).lower()
    published_at = str(article.get("publishedAt", ""))
    if not title:
        return ""

    return f"{title}|{published_at}"


def _parse_callback_data(data: str) -> tuple[str, str] | None:
    if ":" not in data:
        return None
    action, value = data.split(":", 1)
    if not action or not value:
        return None
    return action, value


def _get_source_name(article: Article) -> tuple[str, str]:
    """Return (raw_source_name, escaped_source_name)."""
    source_obj = article.get("source", {})
    if isinstance(source_obj, dict):
        raw = str(source_obj.get("name", "Unknown"))
        return raw, _escape_html(raw)
    return "Unknown", "Unknown"


async def _build_trending_topic_rows(
    chat_id: int, topics: list[str]
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []

    for topic in topics:
        safe_topic = _sanitize_follow_topic(topic)
        if not safe_topic:
            continue

        follow_action = "unfollow" if await is_following_topic(chat_id, safe_topic) else "follow"
        follow_label = "➖ Unfollow" if follow_action == "unfollow" else "➕ Follow"

        # Ensure callback_data stays within Telegram's 64-byte limit.
        for prefix in (f"search:{safe_topic}", f"{follow_action}:{safe_topic}"):
            if len(prefix.encode("utf-8")) > 64:
                # Truncate topic to fit within 64 bytes with the prefix.
                prefix_bytes = prefix.encode("utf-8")
                topic_bytes = safe_topic.encode("utf-8")
                overflow = len(prefix_bytes) - 64
                safe_topic = topic_bytes[: len(topic_bytes) - overflow - 1].decode(
                    "utf-8", errors="ignore"
                )
                follow_action = (
                    "unfollow" if await is_following_topic(chat_id, safe_topic) else "follow"
                )
                follow_label = "➖ Unfollow" if follow_action == "unfollow" else "➕ Follow"
                break

        rows.append(
            [
                InlineKeyboardButton("🔍 Search", callback_data=f"search:{safe_topic}"),
                InlineKeyboardButton(follow_label, callback_data=f"{follow_action}:{safe_topic}"),
            ]
        )

    return rows


def _format_followed_topics(topics: list[str]) -> str:
    if not topics:
        return "You are not following any topics yet."

    lines = ["<b>Your Followed Topics</b>\n"]
    for index, topic in enumerate(topics, 1):
        lines.append(f"{index}. {_escape_html(topic)}")
    return "\n".join(lines)


def _format_news_digest(
    articles: list[Article],
    category: str,
    country: str,
    sources: list[str] | None = None,
) -> str:
    """Format articles into a clean multi-line HTML digest for Telegram.

    Layout (mobile-friendly cards)::

        📰 News Briefing
        Technology · 🇺🇸 US · 8 stories

        1. Headline as a bold link
        Short description…
        ⏱ 2h ago  ·  📍 Reuters  ·  also BBC
        📌 #AI · 🗞 Multi-source

        2. Next headline…
        …
    """
    cat_label = _category_label(category)
    region = _country_display(country)
    # Cap for readability on mobile (still enough for a solid briefing).
    shown = articles[:10]
    divider = "─" * 18
    follow_count = sum(1 for a in shown if a.get("matchedTopics"))

    lines: list[str] = [
        "📰 <b>News Briefing</b>",
        f"{_escape_html(cat_label)}  ·  {_escape_html(region)}  ·  "
        f"{len(shown)} stor{'y' if len(shown) == 1 else 'ies'}",
    ]
    if follow_count:
        lines.append(f"📌 {follow_count} match your follows (shown first)")
    lines.extend([divider, ""])

    for i, article in enumerate(shown, 1):
        title = _escape_html(article.get("title", "No title"))
        url = _safe_url(article.get("url", ""))
        blurb = _article_blurb(article, max_length=140)

        # Title line — bold number + clickable headline (no meta crammed in).
        if url:
            escaped_url = html.escape(url, quote=True)
            lines.append(f'<b>{i}.</b> <a href="{escaped_url}"><b>{title}</b></a>')
        else:
            lines.append(f"<b>{i}. {title}</b>")

        # Always try to show a short description under the headline.
        if blurb:
            lines.append(f"<i>{_escape_html(blurb)}</i>")
        else:
            lines.append("<i>Tap the title or button below to read the full story.</i>")

        lines.append(_format_source_line(article))

        why = article.get("whyTags") or []
        if isinstance(why, list) and why:
            tags = [str(t) for t in why if t]
            lines.append(" · ".join(tags))

        lines.append("")  # blank line between cards

    # Footer
    lines.append(divider)
    footer_bits: list[str] = []
    if sources:
        source_labels: list[str] = []
        for s in sources:
            if s == "newsdata.io":
                source_labels.append("NewsData.io")
            elif s == "rss":
                source_labels.append("RSS")
            else:
                source_labels.append(_escape_html(s))
        if source_labels:
            footer_bits.append(" + ".join(source_labels))
    footer_bits.append("buttons below open full articles")
    lines.append("💡 " + "  ·  ".join(footer_bits))
    return "\n".join(lines)


async def _send_trending_results(
    message_target: Message,
    chat_id: int,
    category: str,
    country: str | None = None,
) -> None:
    status_msg = await message_target.reply_text("📊 Fetching trending topics...")

    try:
        countries = [country] if country else list(COUNTRIES.values())
        trending_topics = await fetch_trending_topics(countries, category)

        await status_msg.delete()

        if not trending_topics:
            await message_target.reply_text(
                f"No trending topics found for {_category_label(category).lower()} right now."
            )
            return

        label = _category_label(category)
        message = f"📈 <b>Trending Topics ({_escape_html(label)})</b>\n\n"
        for index, (topic, count) in enumerate(trending_topics.items(), 1):
            message += f"{index}. <b>{_escape_html(topic.capitalize())}</b> — {count} articles\n"

        message += "\n💡 Use the buttons below to search or follow a topic."

        keyboard_rows = await _build_trending_topic_rows(chat_id, list(trending_topics.keys())[:5])

        await message_target.reply_text(
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None,
        )
    except Exception as exc:
        logger.error("Error fetching trending topics: %s", exc)
        await status_msg.edit_text("🔧 Failed to fetch trending topics. Please try again later.")


async def _clear_chat_messages(
    bot: Bot,
    chat_id: int,
    from_id: int,
    window: int = 150,
    *,
    skip_ids: set[int] | None = None,
) -> tuple[int, int]:
    """Walk recent message IDs and delete everything the bot is allowed to.

    Telegram constraints:
    - Private chats: bot can usually only delete **its own** messages (<48h).
    - Groups: needs delete-message admin rights for others' messages.
    - There is no ``get_chat_history`` API for bots, so we probe a contiguous
      ID range ending at *from_id*.

    ``Forbidden`` on a single ID is **skipped** (often a user message in DMs),
    not a hard stop — otherwise clear would abort on the first user message.

    Returns ``(deleted, skipped_or_errors)``.
    """
    deleted = 0
    skipped = 0
    hard_errors = 0
    skip = skip_ids or set()

    # Probe from the newest ID downward through the window.
    start = max(1, from_id)
    end = max(0, start - window)
    for msg_id in range(start, end, -1):
        if msg_id in skip:
            continue
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted += 1
        except Forbidden:
            # Expected for other users' messages in private chat — keep going.
            skipped += 1
            continue
        except BadRequest as exc:
            s = str(exc).lower()
            if (
                "not found" in s
                or "can't be deleted" in s
                or "message to delete not found" in s
                or "message can't be deleted" in s
                or "message is too old" in s
                or "message identifier is not specified" in s
            ):
                skipped += 1
                continue
            logger.warning("clear_chat BadRequest for message %s: %s", msg_id, exc)
            hard_errors += 1
            if hard_errors > 8:
                return deleted, skipped + hard_errors
        except TelegramError as exc:
            logger.warning("clear_chat TelegramError for message %s: %s", msg_id, exc)
            hard_errors += 1
            if hard_errors > 8:
                return deleted, skipped + hard_errors

    return deleted, skipped + hard_errors
