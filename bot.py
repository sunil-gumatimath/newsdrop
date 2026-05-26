from __future__ import annotations

# pyright: reportMissingImports=false, reportMissingModuleSource=false
import asyncio
import html
import logging
from datetime import time
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config import (
    BREAKING_ALERT_INTERVAL_MINUTES,
    BREAKING_ALERT_KEYWORDS,
    BREAKING_ALERT_RETENTION_DAYS,
    CATEGORIES,
    COUNTRIES,
    DAILY_NEWS_TIME,
    DEFAULT_COUNTRY,
    TELEGRAM_BOT_TOKEN,
)
from database import (
    add_followed_topic,
    add_subscriber,
    check_db_health,
    cleanup_old_breaking_alerts,
    clear_followed_topics,
    get_breaking_news_preference,
    get_followed_topics,
    get_user_prefs,
    is_following_topic,
    is_subscriber,
    load_breaking_news_subscribers,
    load_subscribers,
    mark_breaking_alert_sent,
    remove_followed_topic,
    remove_subscriber,
    set_breaking_news_preference,
    set_user_prefs,
    was_breaking_alert_sent,
)
from message_utils import send_chunked_message
from news_fetcher import (
    APIClientError,
    check_api_health,
    fetch_breaking_news,
    fetch_top_headlines,
    fetch_trending_topics,
    format_search_results,
    get_article_image,
    get_request_count,
    search_news,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SearchRateLimit = dict[int, float]
Article = dict[str, Any]
Prefs = dict[str, str]

_search_rate_limit: SearchRateLimit = {}
SEARCH_COOLDOWN_SECONDS = 10

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


class ReplyTarget(Protocol):
    async def reply_text(self, text: str, **kwargs: Any) -> Message: ...
    async def reply_photo(self, photo: str, **kwargs: Any) -> Message: ...


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


def _get_monotonic_time() -> float:
    return asyncio.get_running_loop().time()


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


def _get_source_name(article: Article) -> str:
    source_obj = article.get("source", {})
    if isinstance(source_obj, dict):
        return _escape_html(source_obj.get("name", "Unknown"))
    return "Unknown"


def _build_article_caption(index: int, article: Article) -> str:
    title = _escape_html(article.get("title", "No title"))
    description = _truncate_text(article.get("description", ""), 150)
    source = _get_source_name(article)

    caption = f"<b>{index}. {title}</b>\n"
    if description:
        caption += f"<i>{_escape_html(description)}</i>\n"
    caption += f"📍 {source}"
    return caption


def _build_read_more_keyboard(article: Article) -> InlineKeyboardMarkup | None:
    url = _safe_url(article.get("url", ""))
    if not url:
        return None

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📖 Read full article", url=url)]]
    )


def _build_trending_topic_rows(
    chat_id: int, topics: list[str]
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []

    for topic in topics:
        safe_topic = _sanitize_follow_topic(topic)
        if not safe_topic:
            continue

        follow_action = (
            "unfollow" if is_following_topic(chat_id, safe_topic) else "follow"
        )
        follow_label = "➖ Unfollow" if follow_action == "unfollow" else "➕ Follow"

        rows.append(
            [
                InlineKeyboardButton("🔍 Search", callback_data=f"search:{safe_topic}"),
                InlineKeyboardButton(
                    follow_label, callback_data=f"{follow_action}:{safe_topic}"
                ),
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


async def _send_article_message(
    message_target: ReplyTarget,
    article: Article,
    index: int,
) -> None:
    caption = _build_article_caption(index, article)
    keyboard = _build_read_more_keyboard(article)
    image_url = get_article_image(article)

    try:
        if image_url:
            await message_target.reply_photo(
                photo=image_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        else:
            await message_target.reply_text(
                caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
    except Exception:
        await message_target.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def _send_article_via_bot(
    bot: Bot,
    chat_id: int,
    article: Article,
    index: int,
) -> None:
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
) -> None:
    status_msg = await message_target.reply_text("📊 Fetching trending topics...")

    try:
        countries = list(COUNTRIES.values())
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

        keyboard_rows = _build_trending_topic_rows(
            chat_id, list(trending_topics.keys())[:5]
        )

        await message_target.reply_text(
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None,
        )
    except Exception as exc:
        logger.error("Error fetching trending topics: %s", exc)
        await status_msg.edit_text(
            "🔧 Failed to fetch trending topics. Please try again later."
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    message = update.effective_message
    if not message:
        return

    welcome = (
        "Welcome to Daily News Bot! 📰\n\n"
        "Use /news to get today's news briefing.\n"
        "Use /subscribe to receive daily news automatically.\n"
        "Use /search <topic> to search for specific news.\n"
        "Use /follow <topic> to follow a topic like AI or crypto.\n"
        "Use /trending tech to view category-specific trends.\n"
        "Use /help to see all commands."
    )
    _ = await message.reply_text(welcome)


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    prefs: Prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    country = prefs.get("country", DEFAULT_COUNTRY)
    category = prefs.get("category", "general")

    status_msg = await message.reply_text("Fetching latest news...")

    try:
        data = await fetch_top_headlines(country, category)
        articles = _get_articles(data)

        if not articles:
            _ = await status_msg.edit_text(
                f"No {_escape_html(category)} news articles found for "
                f"{_escape_html(country.upper())}. Try again later."
            )
            return

        _ = await status_msg.delete()

        cat_label = _category_label(category)
        published_at = str(articles[0].get("publishedAt", ""))
        date_str = published_at[:10] if published_at else "today"
        header = (
            f"📰 <b>Daily News Briefing — {_escape_html(date_str)}</b>\n"
            f"🌍 {_escape_html(cat_label)} Headlines ({_escape_html(country.upper())})\n"
        )
        _ = await message.reply_text(header, parse_mode=ParseMode.HTML)

        for i, article in enumerate(articles[:10], 1):
            await _send_article_message(message, article, i)

        _ = await message.reply_text("Stay informed! 🌍")

    except APIClientError as exc:
        logger.error("News API error fetching news: %s", exc)
        _ = await status_msg.edit_text(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error fetching news: %s", exc)
        _ = await status_msg.edit_text(
            "🔧 An unexpected error occurred. Please try again later."
        )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    if not context.args:
        _ = await message.reply_text("Usage: /search <topic>\nExample: /search bitcoin")
        return

    current_time = _get_monotonic_time()
    last_search = _search_rate_limit.get(chat_id, 0.0)

    if current_time - last_search < SEARCH_COOLDOWN_SECONDS:
        remaining = int(SEARCH_COOLDOWN_SECONDS - (current_time - last_search))
        _ = await message.reply_text(
            f"⏳ Please wait {remaining} second(s) before searching again."
        )
        return

    query = " ".join(context.args).strip()
    if not query:
        _ = await message.reply_text("Usage: /search <topic>\nExample: /search bitcoin")
        return

    prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    country = prefs.get("country", DEFAULT_COUNTRY)

    status_msg = await message.reply_text(f'🔍 Searching for "{query}"...')

    try:
        data = await search_news(query, country)
        results = format_search_results(data, query)
        _ = await status_msg.delete()
        await send_chunked_message(
            message,
            results,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        _search_rate_limit[chat_id] = _get_monotonic_time()
    except APIClientError as exc:
        logger.error("News API error searching news: %s", exc)
        _ = await status_msg.edit_text(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error searching news: %s", exc)
        _ = await status_msg.edit_text(
            "🔧 An unexpected error occurred. Please try again later."
        )


async def follow_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    if not context.args:
        _ = await message.reply_text("Usage: /follow <topic>\nExample: /follow AI")
        return

    topic = _sanitize_follow_topic(" ".join(context.args))
    created, result = add_followed_topic(chat_id, topic)

    if created:
        _ = await message.reply_text(
            f"✅ Now following <b>{_escape_html(result)}</b>.",
            parse_mode=ParseMode.HTML,
        )
    else:
        _ = await message.reply_text(f"⚠️ {result}")


async def unfollow_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    if not context.args:
        _ = await message.reply_text("Usage: /unfollow <topic>\nExample: /unfollow AI")
        return

    topic = _sanitize_follow_topic(" ".join(context.args))
    removed = remove_followed_topic(chat_id, topic)

    if removed:
        _ = await message.reply_text(
            f"✅ Unfollowed <b>{_escape_html(topic)}</b>.",
            parse_mode=ParseMode.HTML,
        )
    else:
        _ = await message.reply_text("⚠️ You are not following that topic.")


async def list_followed_topics(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    del context

    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    topics = get_followed_topics(chat_id)
    _ = await message.reply_text(
        _format_followed_topics(topics),
        parse_mode=ParseMode.HTML,
    )


async def unfollow_all_topics(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    del context

    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    removed_count = clear_followed_topics(chat_id)
    if removed_count > 0:
        _ = await message.reply_text(
            f"✅ Removed all followed topics ({removed_count})."
        )
    else:
        _ = await message.reply_text("You were not following any topics.")


async def set_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    keyboard = [
        [InlineKeyboardButton(display, callback_data=f"country:{code}")]
        for display, code in COUNTRIES.items()
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    current = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    current_code = current.get("country", DEFAULT_COUNTRY)
    current_name = _country_name_from_code(current_code)

    _ = await message.reply_text(
        f"🌍 Current region: <b>{_escape_html(current_name)}</b>\n\nSelect your news region:",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def set_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    keyboard = [
        [InlineKeyboardButton(cat.capitalize(), callback_data=f"category:{cat}")]
        for cat in CATEGORIES
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    current = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    current_cat = current.get("category", "general")

    _ = await message.reply_text(
        f"📂 Current category: <b>{_escape_html(current_cat.capitalize())}</b>\n\n"
        f"Select your news topic:",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    query = update.callback_query
    chat_id = _effective_chat_id(update)
    if not query:
        return

    if chat_id is None:
        await query.answer()
        return

    await query.answer()

    data = query.data or ""
    parsed = _parse_callback_data(data)
    if parsed is None:
        logger.warning("Invalid callback data format: %s", data)
        _ = await query.edit_message_text("⚠️ Invalid selection.")
        return

    action, value = parsed

    if action == "country":
        valid_codes = set(COUNTRIES.values())
        if value not in valid_codes:
            logger.warning("Rejected invalid country code in callback: %s", value)
            _ = await query.edit_message_text("⚠️ Invalid region selection.")
            return

        name = _country_name_from_code(value)
        set_user_prefs(chat_id, country=value)
        _ = await query.edit_message_text(
            f"✅ Region set to <b>{_escape_html(name)}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "category":
        if value not in CATEGORIES:
            logger.warning("Rejected invalid category in callback: %s", value)
            _ = await query.edit_message_text("⚠️ Invalid category selection.")
            return

        set_user_prefs(chat_id, category=value)
        _ = await query.edit_message_text(
            f"✅ Category set to <b>{_escape_html(value.capitalize())}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "breaking":
        enabled = value == "1"
        set_breaking_news_preference(chat_id, enabled)
        status_text = "enabled" if enabled else "disabled"
        _ = await query.edit_message_text(
            f"✅ Breaking news alerts <b>{status_text}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "search":
        topic = _sanitize_follow_topic(value)
        if not topic:
            _ = await query.edit_message_text("⚠️ Invalid search topic.")
            return

        if not query.message:
            _ = await query.edit_message_text("⚠️ Search message is unavailable.")
            return

        prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
        country = prefs.get("country", DEFAULT_COUNTRY)
        status_msg = await query.message.reply_text(f'🔍 Searching for "{topic}"...')

        try:
            data_search = await search_news(topic, country)
            results = format_search_results(data_search, topic)
            _ = await status_msg.delete()
            await send_chunked_message(
                query.message,
                results,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            _search_rate_limit[chat_id] = _get_monotonic_time()
        except APIClientError as exc:
            logger.error("News API error searching news: %s", exc)
            _ = await status_msg.edit_text(str(exc))
        except Exception as exc:
            logger.exception("Unexpected error searching news: %s", exc)
            _ = await status_msg.edit_text(
                "🔧 An unexpected error occurred. Please try again later."
            )
        return

    if action == "follow":
        topic = _sanitize_follow_topic(value)
        created, result = add_followed_topic(chat_id, topic)
        if query.message:
            if created:
                _ = await query.message.reply_text(
                    f"✅ Now following <b>{_escape_html(result)}</b>.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                _ = await query.message.reply_text(f"⚠️ {result}")
        return

    if action == "unfollow":
        topic = _sanitize_follow_topic(value)
        removed = remove_followed_topic(chat_id, topic)
        if query.message:
            if removed:
                _ = await query.message.reply_text(
                    f"✅ Unfollowed <b>{_escape_html(topic)}</b>.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                _ = await query.message.reply_text(
                    "⚠️ You are not following that topic."
                )
        return

    logger.warning("Unhandled callback action: %s", action)
    _ = await query.edit_message_text("⚠️ Unsupported action.")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    if is_subscriber(chat_id):
        _ = await message.reply_text("You are already subscribed to daily news!")
        return

    add_subscriber(chat_id)
    prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    _ = await message.reply_text(
        f"✅ Subscribed! You'll receive daily news at {DAILY_NEWS_TIME} "
        f"for {prefs['category']} news in {prefs['country'].upper()}.\n"
        f"Use /unsubscribe to stop."
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    if not is_subscriber(chat_id):
        _ = await message.reply_text("You are not subscribed to daily news.")
        return

    remove_subscriber(chat_id)
    _ = await message.reply_text("Unsubscribed. You will no longer receive daily news.")


async def preferences(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    country_code = prefs.get("country", DEFAULT_COUNTRY)
    category = prefs.get("category", "general")
    country_name = _country_name_from_code(country_code)
    followed_topics = get_followed_topics(chat_id)

    text = (
        "⚙️ <b>Your Preferences</b>\n\n"
        f"🌍 Region: {_escape_html(country_name)}\n"
        f"📂 Category: {_escape_html(category.capitalize())}\n"
        f"🏷️ Followed topics: {len(followed_topics)}\n\n"
        "Use /setcountry and /setcategory to change these.\n"
        "Use /follows to see your followed topics."
    )
    _ = await message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    message = update.effective_message
    if not message:
        logger.warning("Help command received without an effective message")
        return

    help_text = (
        "<b>Available Commands:</b>\n\n"
        "/start - Start the bot\n"
        "/news - Get news briefing (uses your preferences)\n"
        "/search <topic> - Search for specific news\n"
        "/follow <topic> - Follow a topic like AI or crypto\n"
        "/unfollow <topic> - Stop following a topic\n"
        "/follows - View followed topics\n"
        "/topics - Alias for /follows\n"
        "/unfollowall - Remove all followed topics\n"
        "/subscribe - Enable daily news delivery\n"
        "/unsubscribe - Disable daily news delivery\n"
        "/setcountry - Choose your news region\n"
        "/setcategory - Choose your news topic\n"
        "/trending [category] - View trending topics, e.g. /trending tech\n"
        "/breaking - Toggle breaking news alerts\n"
        "/health - Check bot health status\n"
        "/clear - Cleanup messages in the chat\n"
        "/prefs - View your current preferences\n"
        "/help - Show this help message\n"
        "/commands - Show all commands"
    )

    try:
        _ = await message.reply_text(help_text, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Failed to send /help response")
        _ = await message.reply_text(
            "Available Commands:\n\n"
            "/start - Start the bot\n"
            "/news - Get news briefing\n"
            "/search <topic> - Search news\n"
            "/follow <topic> - Follow a topic\n"
            "/unfollow <topic> - Unfollow a topic\n"
            "/follows - View followed topics\n"
            "/topics - Alias for /follows\n"
            "/unfollowall - Remove all followed topics\n"
            "/subscribe - Enable daily news\n"
            "/unsubscribe - Disable daily news\n"
            "/setcountry - Choose your region\n"
            "/setcategory - Choose your topic\n"
            "/trending [category] - View category trends\n"
            "/breaking - Toggle breaking alerts\n"
            "/health - Check bot health\n"
            "/clear - Cleanup messages in the chat\n"
            "/prefs - View preferences\n"
            "/help - Show this help message\n"
            "/commands - Show all commands"
        )


async def breaking_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    current_enabled = get_breaking_news_preference(chat_id)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔔 Turn ON" if not current_enabled else "🔕 Turn OFF",
                    callback_data=f"breaking:{1 if not current_enabled else 0}",
                )
            ]
        ]
    )

    status_text = "enabled" if current_enabled else "disabled"
    _ = await message.reply_text(
        f"🚨 Breaking news alerts are currently <b>{status_text}</b>.\n\n"
        f"The bot checks every {BREAKING_ALERT_INTERVAL_MINUTES} minute(s) and sends "
        "important stories for your selected region.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    raw_category = " ".join(context.args).strip() if context.args else ""
    category = _resolve_trending_category(raw_category)

    if category is None:
        supported = ", ".join(sorted(TRENDING_CATEGORY_ALIASES.keys()))
        _ = await message.reply_text(
            f"⚠️ Unknown trending category.\n\nTry one of: {supported}"
        )
        return

    await _send_trending_results(message, chat_id, category)


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    message = update.effective_message
    if not message:
        return

    status_msg = await message.reply_text("🏥 Checking bot health...")

    try:
        api_health = await check_api_health()
        db_health = check_db_health()
        request_count, request_limit = get_request_count()

        _ = await status_msg.delete()

        api_emoji = "✅" if api_health["status"] == "healthy" else "❌"
        db_emoji = "✅" if db_health["status"] == "healthy" else "❌"

        health_message = (
            "🏥 <b>Bot Health Status</b>\n\n"
            f"{api_emoji} NewsData.io: {api_health['status']}"
        )

        if api_health["status"] == "healthy":
            health_message += f" ({api_health.get('response_time', 'N/A')})"
        else:
            health_message += f"\n   Error: {api_health.get('error', 'Unknown')}"

        health_message += f"\n{db_emoji} Database: {db_health['status']}"

        if db_health["status"] == "healthy":
            health_message += (
                f"\n   Subscribers: {db_health.get('subscriber_count', '0')}"
            )
            health_message += (
                f"\n   Followed topics: {db_health.get('followed_topic_count', '0')}"
            )
            health_message += f"\n   Breaking alerts tracked: {db_health.get('breaking_alert_count', '0')}"
        else:
            health_message += f"\n   Error: {db_health.get('error', 'Unknown')}"

        health_message += f"\n\n📊 API Requests: {request_count}/{request_limit} today"
        health_message += "\n📊 Cache: Active (5min TTL)\n"
        health_message += "🤖 Bot: Running"

        _ = await message.reply_text(health_message, parse_mode=ParseMode.HTML)

    except Exception as exc:
        logger.error("Error checking health: %s", exc)
        _ = await status_msg.edit_text("🔧 Failed to check health status.")


async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    status_msg = await message.reply_text("🧹 Clearing messages...")
    current_id = status_msg.message_id

    # Try to delete the last 60 messages to clean up the chat
    tasks = []
    for msg_id in range(current_id, current_id - 60, -1):
        tasks.append(context.bot.delete_message(chat_id=chat_id, message_id=msg_id))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    deleted_count = sum(1 for res in results if not isinstance(res, Exception))

    # Send temporary confirmation
    try:
        confirm_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🧹 Cleared {deleted_count} message(s) from this chat."
        )
        await asyncio.sleep(3)
        await confirm_msg.delete()
    except Exception:
        pass


async def send_breaking_news_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = load_breaking_news_subscribers()
    if not subscribers:
        logger.info("No users opted into breaking news alerts.")
        return

    if not BREAKING_ALERT_KEYWORDS:
        logger.info(
            "Breaking news alerts are disabled because no keywords are configured."
        )
        return

    country_to_chats: dict[str, list[int]] = {}
    for chat_id in subscribers:
        prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
        country = prefs.get("country", DEFAULT_COUNTRY)
        country_to_chats.setdefault(country, []).append(chat_id)

    countries = list(country_to_chats.keys())
    if not countries:
        return

    logger.info(
        "Checking breaking news for %s opted-in user(s) across %s region(s).",
        len(subscribers),
        len(countries),
    )

    try:
        cleanup_old_breaking_alerts(BREAKING_ALERT_RETENTION_DAYS)
        articles = await fetch_breaking_news(countries, BREAKING_ALERT_KEYWORDS)
    except APIClientError as exc:
        logger.warning("News API error checking breaking alerts: %s", exc)
        return
    except Exception as exc:
        logger.exception("Unexpected error checking breaking alerts: %s", exc)
        return

    if not articles:
        logger.info("No breaking news matches found.")
        return

    sent_count = 0
    for article in articles[:20]:
        country = str(article.get("country", ""))
        chat_ids = country_to_chats.get(country, [])
        article_key = _get_article_key(article)

        if not article_key:
            continue

        for chat_id in chat_ids:
            title = str(article.get("title", ""))
            url = _safe_url(article.get("url", ""))

            if was_breaking_alert_sent(chat_id, article_key):
                continue

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🚨 <b>Breaking News Alert</b>",
                    parse_mode=ParseMode.HTML,
                )
                await _send_article_via_bot(context.bot, chat_id, article, 1)
                if mark_breaking_alert_sent(chat_id, article_key, url, title):
                    sent_count += 1
            except Exception as exc:
                logger.exception(
                    "Failed to send breaking alert to %s: %s",
                    chat_id,
                    exc,
                )

    logger.info("Sent %s breaking news alert(s).", sent_count)


async def send_daily_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = load_subscribers()
    if not subscribers:
        logger.info("No subscribers to send daily news to.")
        return

    logger.info("Sending daily news to %s subscribers...", len(subscribers))

    for chat_id in subscribers:
        try:
            prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
            country = prefs.get("country", DEFAULT_COUNTRY)
            category = prefs.get("category", "general")

            data = await fetch_top_headlines(country, category)
            articles = _get_articles(data)

            if not articles:
                _ = await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"No {_escape_html(category)} news articles found for "
                        f"{_escape_html(country.upper())}."
                    ),
                )
                continue

            cat_label = _category_label(category)
            published_at = str(articles[0].get("publishedAt", ""))
            date_str = published_at[:10] if published_at else "today"
            header = (
                f"📰 <b>Daily News Briefing — {_escape_html(date_str)}</b>\n"
                f"🌍 {_escape_html(cat_label)} Headlines ({_escape_html(country.upper())})\n"
            )
            _ = await context.bot.send_message(
                chat_id=chat_id,
                text=header,
                parse_mode=ParseMode.HTML,
            )

            for i, article in enumerate(articles[:10], 1):
                await _send_article_via_bot(context.bot, chat_id, article, i)

            _ = await context.bot.send_message(
                chat_id=chat_id, text="Stay informed! 🌍"
            )

        except APIClientError as exc:
            logger.error("News API error sending daily news to %s: %s", chat_id, exc)
            try:
                _ = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Error fetching today's news: {exc}",
                )
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Failed to send news to %s: %s", chat_id, exc)


async def _setup_commands(
    application: Application[Any, Any, Any, Any, Any, Any],
) -> None:
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("news", "Get latest news briefing"),
        BotCommand("search", "Search news by topic"),
        BotCommand("follow", "Follow a topic like AI or crypto"),
        BotCommand("unfollow", "Unfollow a topic"),
        BotCommand("follows", "View followed topics"),
        BotCommand("topics", "Alias for followed topics"),
        BotCommand("unfollowall", "Remove all followed topics"),
        BotCommand("subscribe", "Enable daily news delivery"),
        BotCommand("unsubscribe", "Disable daily news delivery"),
        BotCommand("setcountry", "Choose your news region"),
        BotCommand("setcategory", "Choose your news topic"),
        BotCommand("prefs", "View your preferences"),
        BotCommand("breaking", "Toggle breaking news alerts"),
        BotCommand("trending", "View trending topics by category"),
        BotCommand("health", "Check bot health status"),
        BotCommand("clear", "Cleanup messages in the chat"),
        BotCommand("help", "Show all commands"),
        BotCommand("commands", "Show all commands"),
    ]
    await application.bot.set_my_commands(commands)


async def error_handler(
    update: Update | object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.exception("Unhandled bot error", exc_info=context.error)

    if not isinstance(update, Update):
        return

    telegram_update = cast(Update, update)
    message = telegram_update.effective_message
    if message:
        try:
            _ = await message.reply_text(
                "🔧 Something went wrong while processing that command. Please try again."
            )
        except Exception:
            logger.exception("Failed to send error message to Telegram user")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables")
        return

    try:
        daily_time = _parse_daily_time(DAILY_NEWS_TIME)
    except ValueError as exc:
        logger.error(str(exc))
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("follow", follow_topic))
    app.add_handler(CommandHandler("unfollow", unfollow_topic))
    app.add_handler(CommandHandler("follows", list_followed_topics))
    app.add_handler(CommandHandler("topics", list_followed_topics))
    app.add_handler(CommandHandler("unfollowall", unfollow_all_topics))
    app.add_handler(CommandHandler("setcountry", set_country))
    app.add_handler(CommandHandler("setcategory", set_category))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("prefs", preferences))
    app.add_handler(CommandHandler("breaking", breaking_toggle))
    app.add_handler(CommandHandler("trending", trending))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("clear", clear_chat))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("commands", help_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)
    app.post_init = _setup_commands

    if app.job_queue is None:
        logger.error("Job queue is unavailable. Install job-queue dependencies.")
        return

    app.job_queue.run_daily(send_daily_news, time=daily_time)

    if BREAKING_ALERT_INTERVAL_MINUTES > 0:
        app.job_queue.run_repeating(
            send_breaking_news_alerts,
            interval=BREAKING_ALERT_INTERVAL_MINUTES * 60,
            first=60,
        )
        logger.info(
            "Breaking news alerts scheduled every %s minute(s)",
            BREAKING_ALERT_INTERVAL_MINUTES,
        )
    else:
        logger.info("Breaking news alerts are disabled by configuration.")

    logger.info("Daily news scheduled for %s", DAILY_NEWS_TIME)
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
