from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime, time
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
    COUNTRIES,
    NEWS_COOLDOWN_SECONDS as _NEWS_COOLDOWN_SECONDS,
    SEARCH_COOLDOWN_SECONDS as _SEARCH_COOLDOWN_SECONDS,
)
from ..database import (
    is_following_topic,
)
from ..news_fetcher import (
    fetch_trending_topics,
    get_article_image,
)

logger = logging.getLogger(__name__)

Article = dict[str, Any]
NewsResponse = dict[str, Any]
Prefs = dict[str, str]


class DigestResult(NamedTuple):
    digest: str | None
    empty_message: str | None


# Per-user cooldown scopes. Backed by ``newsdrop.state`` (Redis when
# ``REDIS_URL`` is set, in-memory otherwise). The cooldown window length is
# tunable via the ``NEWS_COOLDOWN_SECONDS`` / ``SEARCH_COOLDOWN_SECONDS`` env
# vars (see config.py); 0 disables the cooldown entirely.
SEARCH_RATE_LIMIT_SCOPE = "search"
SEARCH_COOLDOWN_SECONDS = _SEARCH_COOLDOWN_SECONDS

NEWS_RATE_LIMIT_SCOPE = "news"
NEWS_COOLDOWN_SECONDS = _NEWS_COOLDOWN_SECONDS

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
    return text[: max_length - 3].rstrip() + "..."


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


def _country_name_from_code(code: str) -> str:
    return next((name for name, value in COUNTRIES.items() if value == code), code)


def _effective_chat_id(update: Update) -> int | None:
    chat = update.effective_chat
    return chat.id if chat else None


def _parse_daily_time(value: str) -> time:
    try:
        parts = value.strip().split(":")
        if len(parts) != 2:
            raise ValueError("Expected HH:MM format")

        hour, minute = map(int, parts)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Hour or minute out of range")

        return time(hour, minute)
    except Exception as exc:
        raise ValueError(
            "DAILY_NEWS_TIME must be in 24-hour HH:MM format, for example 08:00"
        ) from exc


def _get_articles(payload: dict[str, Any]) -> list[Article]:
    articles = payload.get("articles", [])
    return articles if isinstance(articles, list) else []


def _build_digest_payload(
    data: NewsResponse,
    category: str,
    country: str,
    followed_topics: list[str] | None = None,
) -> DigestResult:
    """Return either a formatted HTML digest or an empty-state message.

    Returns ``DigestResult(digest, None)`` when articles are available and
    ``DigestResult(None, empty_message)`` otherwise. Centralizes the no-results
    copy shared by ``/news`` and the scheduled daily job.
    """
    articles = _get_articles(data)
    if articles:
        sources = data.get("sources", []) if isinstance(data, dict) else []
        return DigestResult(_format_news_digest(articles, category, country, followed_topics, sources), None)

    sources_used = data.get("sources", []) if isinstance(data, dict) else []
    if sources_used:
        hint = "Try a different region or category with /setcountry /setcategory."
    else:
        hint = (
            "The news service may be temporarily unavailable. "
            "Try again later, or use /search &lt;topic&gt; for specific news."
        )

    return DigestResult(None,
        f"No {_escape_html(category)} news articles found for "
        f"{_escape_html(country.upper())} right now.\n\n{hint}"
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


def _build_article_caption(index: int, article: Article) -> str:
    title = _escape_html(article.get("title", "No title"))
    description = _truncate_text(article.get("description", ""), 150)
    _, source_escaped = _get_source_name(article)
    rel_time = _format_relative_time(str(article.get("publishedAt", "")))

    caption = f"<b>{index}. {title}</b>\n"
    if description:
        caption += f"<i>{_escape_html(description)}</i>\n"
    if rel_time:
        caption += f"⏱ {rel_time}\n"
    caption += f"📍 {source_escaped}"
    return caption


def _build_read_more_keyboard(article: Article) -> InlineKeyboardMarkup | None:
    url = _safe_url(article.get("url", ""))
    if not url:
        return None

    return InlineKeyboardMarkup([[InlineKeyboardButton("📖 Read full article", url=url)]])


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
                safe_topic = topic_bytes[:len(topic_bytes) - overflow - 1].decode("utf-8", errors="ignore")
                follow_action = "unfollow" if await is_following_topic(chat_id, safe_topic) else "follow"
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
    followed_topics: list[str] | None = None,
    sources: list[str] | None = None,
) -> str:
    """Format articles into clean HTML card digest message.

    Each article is rendered as a compact card: clickable bold title with
    inline time/source meta on the same line, an indented description
    below, and blank-line separators between cards.  If *followed_topics*
    is provided, a highlight section is appended showing which of the
    user's followed topics appear in today's headlines.
    """
    cat_label = _category_label(category)
    shown = articles[:20]

    lines: list[str] = [
        f"📰 <b>Daily News Briefing</b>  ·  {_escape_html(cat_label)} "
        f"({_escape_html(country.upper())})  ·  {len(shown)} articles",
        "",
    ]

    for i, article in enumerate(shown, 1):
        title = _escape_html(article.get("title", "No title"))
        _, source_escaped = _get_source_name(article)
        rel_time = _format_relative_time(str(article.get("publishedAt", "")))
        url = _safe_url(article.get("url", ""))
        description = _truncate_text(article.get("description", ""), 150)

        # Meta parts inline with title
        meta_parts: list[str] = []
        if rel_time:
            meta_parts.append(f"⏱{rel_time}")
        meta_parts.append(f"📍{source_escaped}")
        meta_str = " · ".join(meta_parts)

        # Card header: number + clickable bold title + inline meta
        if url:
            escaped_url = html.escape(url, quote=True)
            lines.append(f'<b>{i}.</b> <a href="{escaped_url}"><b>{title}</b></a>  ·  {meta_str}')
        else:
            lines.append(f"<b>{i}.</b> <b>{title}</b>  ·  {meta_str}")

        # Description on its own line, clean indent
        if description:
            lines.append(f"   {_escape_html(description)}")

        lines.append("")

    # Followed-topics highlight: zero-cost filter over already-fetched
    # articles.  Shows which of the user's interests appear in today's
    # headlines so /follow has tangible value in the daily briefing.
    if followed_topics:
        matched: list[tuple[str, str]] = []
        for topic in followed_topics:
            q = topic.lower().strip()
            if not q:
                continue
            # Word-boundary match to avoid false positives like "ai"
            # matching "rain" or "trail".  Same pattern used by
            # fetch_breaking_news in news_fetcher.py.
            pattern = re.compile(rf"\b{re.escape(q)}\b")
            for article in shown:
                blob = f"{article.get('title', '')} {article.get('description', '')}"
                if pattern.search(blob) or q in blob.lower():
                    matched.append((topic, _escape_html(article.get("title", "No title"))))
                    break

        if matched:
            lines.append("📌 <b>From your followed topics</b>")
            lines.append("")
            for topic, title in matched[:5]:
                lines.append(f"  · #{_escape_html(topic)} — {title}")
            lines.append("")

    # Footer with source attribution
    footer_parts: list[str] = []
    if sources:
        source_labels = []
        for s in sources:
            if s == "newsdata.io":
                source_labels.append("NewsData.io")
            elif s == "rss":
                source_labels.append("RSS")
            else:
                source_labels.append(_escape_html(s))
        footer_parts.append(f"via {' + '.join(source_labels)}")
    footer_parts.append("/search topic · /prefs to customize")
    lines.append("💡 " + " · ".join(footer_parts))
    return "\n".join(lines)


async def _send_article(
    bot: Bot,
    chat_id: int,
    article: Article,
    index: int,
) -> None:
    """Send an article to a chat via the bot (supports photo + caption fallback)."""
    caption = _build_article_caption(index, article)
    keyboard = _build_read_more_keyboard(article)
    image_url = get_article_image(article)

    try:
        if image_url:
            await bot.send_photo(
                chat_id=chat_id,
                photo=image_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
    except Exception:
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


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
    bot: Bot, chat_id: int, from_id: int, window: int = 60
) -> tuple[int, int]:
    """Walk ``range(from_id, from_id - window, -1)`` and try to delete each.

    The bot must have admin (delete-message) rights in the chat for this to
    work. Telegram only lets bots delete messages that are <48h old, so most
    failures are expected and silently skipped. The first ``Forbidden`` aborts
    the loop and posts a user-visible explanation; a flood of other errors is
    capped at 5 so a single cleanup can never lock the bot in a hot loop.

    Returns ``(deleted, errors)`` — the number of successful deletes and the
    number of non-benign Telegram errors encountered. Benign ``BadRequest``
    cases ("not found" / "can't be deleted" / "message is too old") are
    swallowed without counting toward the error budget.
    """
    deleted = 0
    error_count = 0

    # python-telegram-bot 22.7 does not expose Bot.get_chat_history, so we
    # approximate "recent messages" by trying to delete a contiguous ID range
    # around the trigger message. Non-existent IDs and already-deleted
    # messages are filtered out by the BadRequest handling below.
    for msg_id in range(from_id, from_id - window, -1):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted += 1
        except Forbidden:
            # Bot lacks admin rights, or the user is in a DM. Stop and tell
            # them why we can't proceed — silent partial deletes are worse
            # than a clear error.
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text="❌ I need admin rights in this chat to delete messages.",
                )
            except TelegramError as send_exc:
                logger.warning("clear_chat could not post Forbidden notice: %s", send_exc)
            return deleted, error_count
        except BadRequest as exc:
            s = str(exc).lower()
            if "not found" in s or "can't be deleted" in s or "message is too old" in s:
                # Benign: already gone, never ours, or past the 48h window.
                continue
            logger.warning("clear_chat BadRequest for message %s: %s", msg_id, exc)
            error_count += 1
            if error_count > 5:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="⚠️ Stopped after 5 errors — chat may be too active.",
                    )
                except TelegramError as send_exc:
                    logger.warning("clear_chat could not post stop notice: %s", send_exc)
                return deleted, error_count
        except TelegramError as exc:
            logger.warning("clear_chat TelegramError for message %s: %s", msg_id, exc)
            error_count += 1
            if error_count > 5:
                return deleted, error_count

    return deleted, error_count
