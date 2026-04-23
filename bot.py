from __future__ import annotations

# pyright: reportMissingImports=false, reportMissingModuleSource=false
import asyncio
import html
import logging
from datetime import time
from typing import Any, Protocol
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
    CATEGORIES,
    COUNTRIES,
    DAILY_NEWS_TIME,
    DEFAULT_COUNTRY,
    TELEGRAM_BOT_TOKEN,
)
from database import (
    add_subscriber,
    get_user_prefs,
    is_subscriber,
    load_subscribers,
    remove_subscriber,
    set_user_prefs,
    get_breaking_news_preference,
    set_breaking_news_preference,
    check_db_health,
)
from message_utils import send_chunked_message
from news_fetcher import (
    APIClientError,
    fetch_top_headlines,
    format_search_results,
    get_article_image,
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

# Rate limiting for breaking news: {chat_id: last_sent_timestamp}
_breaking_rate_limit: dict[int, float] = {}


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


def _country_name_from_code(code: str) -> str:
    return next((name for name, value in COUNTRIES.items() if value == code), code)


def _effective_chat_id(update: Update) -> int | None:
    chat = update.effective_chat
    return chat.id if chat else None


def _get_articles(payload: dict[str, Any]) -> list[Article]:
    articles = payload.get("articles", [])
    return articles if isinstance(articles, list) else []


def _parse_callback_data(data: str) -> tuple[str, str] | None:
    if ":" not in data:
        return None
    action, value = data.split(":", 1)
    if not action or not value:
        return None
    return action, value


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    if not update.message:
        return

    welcome = (
        "Welcome to Daily News Bot! 📰\n\n"
        "Use /news to get today's news briefing.\n"
        "Use /subscribe to receive daily news automatically.\n"
        "Use /search <topic> to search for specific news.\n"
        "Use /setcountry to pick your region.\n"
        "Use /setcategory to choose topics.\n"
        "Use /help to see all commands."
    )
    _ = await update.message.reply_text(welcome)


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    if not update.message:
        return

    chat_id = _effective_chat_id(update)
    if chat_id is None:
        return

    prefs: Prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    country = prefs.get("country", DEFAULT_COUNTRY)
    category = prefs.get("category", "general")

    status_msg = await update.message.reply_text("Fetching latest news...")

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

        cat_label = category.capitalize() if category != "general" else "Top"
        published_at = str(articles[0].get("publishedAt", ""))
        date_str = published_at[:10] if published_at else "today"
        header = (
            f"📰 <b>Daily News Briefing — {_escape_html(date_str)}</b>\n"
            f"🌍 {_escape_html(cat_label)} Headlines ({_escape_html(country.upper())})\n"
        )
        _ = await update.message.reply_text(header, parse_mode=ParseMode.HTML)

        for i, article in enumerate(articles[:10], 1):
            await _send_article_message(update.message, article, i)

        _ = await update.message.reply_text("Stay informed! 🌍")

    except APIClientError as exc:
        logger.error("News API error fetching news: %s", exc)
        _ = await status_msg.edit_text(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error fetching news: %s", exc)
        _ = await status_msg.edit_text(
            "🔧 An unexpected error occurred. Please try again later."
        )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    chat_id = _effective_chat_id(update)
    if chat_id is None:
        return

    if not context.args:
        _ = await update.message.reply_text(
            "Usage: /search <topic>\nExample: /search bitcoin"
        )
        return

    current_time = _get_monotonic_time()
    last_search = _search_rate_limit.get(chat_id, 0.0)

    if current_time - last_search < SEARCH_COOLDOWN_SECONDS:
        remaining = int(SEARCH_COOLDOWN_SECONDS - (current_time - last_search))
        _ = await update.message.reply_text(
            f"⏳ Please wait {remaining} second(s) before searching again."
        )
        return

    query = " ".join(context.args).strip()
    if not query:
        _ = await update.message.reply_text(
            "Usage: /search <topic>\nExample: /search bitcoin"
        )
        return

    status_msg = await update.message.reply_text(f'🔍 Searching for "{query}"...')

    try:
        data = await search_news(query)
        results = format_search_results(data, query)
        _ = await status_msg.delete()
        await send_chunked_message(
            update.message,
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


async def set_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    if not update.message:
        return

    chat_id = _effective_chat_id(update)
    if chat_id is None:
        return

    keyboard = [
        [InlineKeyboardButton(display, callback_data=f"country:{code}")]
        for display, code in COUNTRIES.items()
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    current = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    current_code = current.get("country", DEFAULT_COUNTRY)
    current_name = _country_name_from_code(current_code)

    _ = await update.message.reply_text(
        f"🌍 Current region: <b>{_escape_html(current_name)}</b>\n\nSelect your news region:",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def set_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    if not update.message:
        return

    chat_id = _effective_chat_id(update)
    if chat_id is None:
        return

    keyboard = [
        [InlineKeyboardButton(cat.capitalize(), callback_data=f"category:{cat}")]
        for cat in CATEGORIES
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    current = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    current_cat = current.get("category", "general")

    _ = await update.message.reply_text(
        f"📂 Current category: <b>{_escape_html(current_cat.capitalize())}</b>\n\n"
        f"Select your news topic:",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    query = update.callback_query
    if not query:
        return

    chat_id = _effective_chat_id(update)
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

    logger.warning("Unhandled callback action: %s", action)
    _ = await query.edit_message_text("⚠️ Unsupported action.")

    elif data.startswith("breaking:"):
        parts = data.split(":")
        if len(parts) < 2:
            logger.warning(f"Invalid callback data format: {data}")
            return
        enabled = parts[1] == "1"
        set_breaking_news_preference(chat_id, enabled)
        status_text = "enabled" if enabled else "disabled"
        await query.edit_message_text(
            f"✅ Breaking news alerts <b>{status_text}</b>", parse_mode="HTML"
        )

    elif data.startswith("search:"):
        parts = data.split(":")
        if len(parts) < 2:
            logger.warning(f"Invalid callback data format: {data}")
            return
        topic = parts[1]
        
        # Trigger search for the topic
        status_msg = await query.message.reply_text(f'🔍 Searching for "{topic}"...')
        
        try:
            data_search = await search_news(topic)
            results = format_search_results(data_search, topic)
            await status_msg.delete()
            await send_chunked_message(
                query.message, results, parse_mode="HTML", disable_web_page_preview=True
            )
            # Update rate limit timestamp after successful search
            _search_rate_limit[chat_id] = context._application._loop.time()
        except APIClientError as e:
            logger.error("News API error searching news: %s", e)
            await status_msg.edit_text(str(e))
        except Exception as e:
            logger.error("Unexpected error searching news: %s", e)
            await status_msg.edit_text(
                "🔧 An unexpected error occurred. Please try again later."
            )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    if not update.message:
        return

    chat_id = _effective_chat_id(update)
    if chat_id is None:
        return

    if is_subscriber(chat_id):
        _ = await update.message.reply_text("You are already subscribed to daily news!")
        return

    add_subscriber(chat_id)
    prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    _ = await update.message.reply_text(
        f"✅ Subscribed! You'll receive daily news at {DAILY_NEWS_TIME} "
        f"for {prefs['category']} news in {prefs['country'].upper()}.\n"
        f"Use /unsubscribe to stop."
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    if not update.message:
        return

    chat_id = _effective_chat_id(update)
    if chat_id is None:
        return

    if not is_subscriber(chat_id):
        _ = await update.message.reply_text("You are not subscribed to daily news.")
        return

    remove_subscriber(chat_id)
    _ = await update.message.reply_text(
        "Unsubscribed. You will no longer receive daily news."
    )


async def preferences(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    if not update.message:
        return

    chat_id = _effective_chat_id(update)
    if chat_id is None:
        return

    prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    country_code = prefs.get("country", DEFAULT_COUNTRY)
    category = prefs.get("category", "general")
    country_name = _country_name_from_code(country_code)

    text = (
        "⚙️ <b>Your Preferences</b>\n\n"
        f"🌍 Region: {_escape_html(country_name)}\n"
        f"📂 Category: {_escape_html(category.capitalize())}\n\n"
        "Use /setcountry and /setcategory to change these."
    )
    _ = await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context

    if not update.message:
        return

    help_text = (
        "<b>Available Commands:</b>\n\n"
        "/start - Start the bot\n"
        "/news - Get news briefing (uses your preferences)\n"
        "/search <topic> - Search for specific news\n"
        "/subscribe - Enable daily news delivery\n"
        "/unsubscribe - Disable daily news delivery\n"
        "/setcountry - Choose your news region\n"
        "/setcategory - Choose your news topic\n"
        "/prefs - View your current preferences\n"
        "/breaking - Toggle breaking news alerts\n"
        "/trending - View trending topics\n"
        "/health - Check bot health status\n"
        "/help - Show this help message"
    )
    _ = await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


async def breaking_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    current_enabled = get_breaking_news_preference(chat_id)
    
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'🔔 Turn ON' if not current_enabled else '🔕 Turn OFF'}",
                    callback_data=f"breaking:{1 if not current_enabled else 0}",
                )
            ]
        ]
    )
    
    status_text = "enabled" if current_enabled else "disabled"
    await update.message.reply_text(
        f"🚨 Breaking news alerts are currently <b>{status_text}</b>.\n\n"
        f"Breaking news will be pushed immediately when important stories are detected.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status_msg = await update.message.reply_text("📊 Fetching trending topics...")
    
    try:
        countries = list(COUNTRIES.values())
        trending_topics = await fetch_trending_topics(countries)
        
        await status_msg.delete()
        
        if not trending_topics:
            await update.message.reply_text("No trending topics found at the moment.")
            return
        
        message = "📈 <b>Trending Topics (Global)</b>\n\n"
        for i, (topic, count) in enumerate(trending_topics.items(), 1):
            message += f"{i}. <b>{topic.capitalize()}</b> — {count} articles\n"
        
        message += "\n💡 Click a topic to search for related news:"
        
        keyboard = []
        for topic in list(trending_topics.keys())[:5]:
            keyboard.append([InlineKeyboardButton(topic.capitalize(), callback_data=f"search:{topic}")])
        
        await update.message.reply_text(message, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        
    except Exception as e:
        logger.error("Error fetching trending topics: %s", e)
        await status_msg.edit_text("🔧 Failed to fetch trending topics. Please try again later.")


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status_msg = await update.message.reply_text("🏥 Checking bot health...")
    
    try:
        # Check API health
        api_health = await check_api_health()
        db_health = check_db_health()
        
        await status_msg.delete()
        
        api_emoji = "✅" if api_health["status"] == "healthy" else "❌"
        db_emoji = "✅" if db_health["status"] == "healthy" else "❌"
        
        message = (
            f"🏥 <b>Bot Health Status</b>\n\n"
            f"{api_emoji} NewsData.io: {api_health['status']}"
        )
        
        if api_health["status"] == "healthy":
            message += f" ({api_health.get('response_time', 'N/A')})"
        else:
            message += f"\n   Error: {api_health.get('error', 'Unknown')}"
        
        message += f"\n{db_emoji} Database: {db_health['status']}"
        
        if db_health["status"] == "healthy":
            message += f"\n   Subscribers: {db_health.get('subscriber_count', '0')}"
        else:
            message += f"\n   Error: {db_health.get('error', 'Unknown')}"
        
        message += f"\n\n📊 Cache: Active (5min TTL)\n"
        message += f"🤖 Bot: Running"
        
        await update.message.reply_text(message, parse_mode="HTML")
        
    except Exception as e:
        logger.error("Error checking health: %s", e)
        await status_msg.edit_text("🔧 Failed to check health status.")


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

            cat_label = category.capitalize() if category != "general" else "Top"
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
        BotCommand("subscribe", "Enable daily news delivery"),
        BotCommand("unsubscribe", "Disable daily news delivery"),
        BotCommand("setcountry", "Choose your news region"),
        BotCommand("setcategory", "Choose your news topic"),
        BotCommand("prefs", "View your preferences"),
        BotCommand("breaking", "Toggle breaking news alerts"),
        BotCommand("trending", "View trending topics"),
        BotCommand("health", "Check bot health status"),
        BotCommand("help", "Show all commands"),
    ]
    await application.bot.set_my_commands(commands)


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
    app.add_handler(CommandHandler("setcountry", set_country))
    app.add_handler(CommandHandler("setcategory", set_category))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("prefs", preferences))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.post_init = _setup_commands

    if app.job_queue is None:
        logger.error("Job queue is unavailable. Install job-queue dependencies.")
        return

    app.job_queue.run_daily(send_daily_news, time=daily_time)

    logger.info("Daily news scheduled for %s", DAILY_NEWS_TIME)
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
