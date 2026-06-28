from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..config import (
    BREAKING_ALERT_INTERVAL_MINUTES,
    CATEGORIES,
    COUNTRIES,
    DAILY_NEWS_TIME,
    DEFAULT_COUNTRY,
)
from ..database import (
    add_followed_topic,
    add_subscriber,
    check_db_health,
    get_breaking_news_preference,
    get_followed_topics,
    get_user_prefs,
    is_subscriber,
    remove_followed_topic,
    remove_subscriber,
)
from ..message_utils import send_chunked_message
from ..metrics import (
    COMMAND_BREAKING_TOGGLE,
    COMMAND_FOLLOW,
    COMMAND_HEALTH,
    COMMAND_NEWS,
    COMMAND_SEARCH,
    COMMAND_SUBSCRIBE,
    COMMAND_TOTAL,
    COMMAND_TRENDING,
    COMMAND_UNFOLLOW,
    COMMAND_UNSUBSCRIBE,
    NEWS_API_ERRORS,
    all_metrics,
    increment,
)
from ..news_fetcher import (
    APIClientError,
    check_api_health,
    fetch_top_headlines,
    format_search_results,
    get_request_count,
    search_news,
)
from ..state import (
    rate_limit_check,
    rate_limit_record,
)
from .helpers import (
    NEWS_COOLDOWN_SECONDS,
    NEWS_RATE_LIMIT_SCOPE,
    SEARCH_COOLDOWN_SECONDS,
    SEARCH_RATE_LIMIT_SCOPE,
    TRENDING_CATEGORY_ALIASES,
    Prefs,
    _build_digest_payload,
    _country_name_from_code,
    _effective_chat_id,
    _escape_html,
    _format_followed_topics,
    _resolve_trending_category,
    _sanitize_follow_topic,
    _send_trending_results,
    logger,
)


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    await increment(COMMAND_TOTAL)

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


async def news(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/news`` — send a single compact digest with all top headlines.

    Instead of spamming 10+ individual messages (photo + caption + button),
    this builds one well-formatted HTML digest with clickable article links,
    brief descriptions, source, and relative time — all in one message.

    A per-user cooldown (``NEWS_COOLDOWN_SECONDS``) protects the
    NewsData.io free-tier budget from spam.  The user's followed topics
    are highlighted in a separate section so ``/follow`` has tangible
    value in the daily briefing.
    """
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    await increment(COMMAND_TOTAL)
    await increment(COMMAND_NEWS)

    # Per-user cooldown — same pattern as /search, but with a longer
    # window since /news fetches a full digest (higher API cost).
    if await rate_limit_check(NEWS_RATE_LIMIT_SCOPE, chat_id, NEWS_COOLDOWN_SECONDS):
        _ = await message.reply_text(
            f"⏳ You're on a cooldown. Try again in {NEWS_COOLDOWN_SECONDS} second(s). "
            "Use /search for specific topics in the meantime."
        )
        return

    prefs: Prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    country = prefs.get("country", DEFAULT_COUNTRY)
    category = prefs.get("category", "general")
    followed = await get_followed_topics(chat_id)

    status_msg = await message.reply_text("📰 Fetching latest news...")

    try:
        data = await fetch_top_headlines(country, category)
        digest, empty_message = _build_digest_payload(data, category, country, followed)

        if empty_message:
            _ = await status_msg.edit_text(
                empty_message,
                parse_mode=ParseMode.HTML,
            )
            return

        assert digest is not None
        if len(digest) <= 4096:
            await status_msg.edit_text(
                digest,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        else:
            await status_msg.delete()
            await send_chunked_message(
                message,
                digest,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        # Record the successful call for cooldown tracking.
        await rate_limit_record(NEWS_RATE_LIMIT_SCOPE, chat_id, NEWS_COOLDOWN_SECONDS)

    except APIClientError as exc:
        await increment(NEWS_API_ERRORS)
        logger.error("News API error fetching news: %s", exc)
        _ = await status_msg.edit_text(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error fetching news: %s", exc)
        _ = await status_msg.edit_text("🔧 An unexpected error occurred. Please try again later.")


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    await increment(COMMAND_TOTAL)
    await increment(COMMAND_SEARCH)

    if not context.args:
        _ = await message.reply_text("Usage: /search <topic>\nExample: /search bitcoin")
        return

    if await rate_limit_check(SEARCH_RATE_LIMIT_SCOPE, chat_id, SEARCH_COOLDOWN_SECONDS):
        _ = await message.reply_text(
            f"⏳ Please wait {SEARCH_COOLDOWN_SECONDS} second(s) before searching again."
        )
        return

    query = " ".join(context.args).strip()
    if not query:
        _ = await message.reply_text("Usage: /search <topic>\nExample: /search bitcoin")
        return

    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
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
        await rate_limit_record(SEARCH_RATE_LIMIT_SCOPE, chat_id, SEARCH_COOLDOWN_SECONDS)
    except APIClientError as exc:
        await increment(NEWS_API_ERRORS)
        logger.error("News API error searching news: %s", exc)
        _ = await status_msg.edit_text(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error searching news: %s", exc)
        _ = await status_msg.edit_text("🔧 An unexpected error occurred. Please try again later.")


async def follow_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    await increment(COMMAND_TOTAL)
    await increment(COMMAND_FOLLOW)

    if not context.args:
        _ = await message.reply_text("Usage: /follow <topic>\nExample: /follow AI")
        return

    topic = _sanitize_follow_topic(" ".join(context.args))
    created, result = await add_followed_topic(chat_id, topic)

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

    await increment(COMMAND_TOTAL)
    await increment(COMMAND_UNFOLLOW)

    if not context.args:
        _ = await message.reply_text("Usage: /unfollow <topic>\nExample: /unfollow AI")
        return

    topic = _sanitize_follow_topic(" ".join(context.args))
    removed = await remove_followed_topic(chat_id, topic)

    if removed:
        _ = await message.reply_text(
            f"✅ Unfollowed <b>{_escape_html(topic)}</b>.",
            parse_mode=ParseMode.HTML,
        )
    else:
        _ = await message.reply_text("⚠️ You are not following that topic.")


async def list_followed_topics(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    topics = await get_followed_topics(chat_id)
    _ = await message.reply_text(
        _format_followed_topics(topics),
        parse_mode=ParseMode.HTML,
    )


async def unfollow_all_topics(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/unfollowall`` — ask for confirmation, then remove ALL followed topics."""
    message = update.effective_message
    user = update.effective_user
    chat_id = _effective_chat_id(update)
    if not message or not user or chat_id is None:
        return

    keyboard = [
        [
            InlineKeyboardButton(
                "Yes, remove all topics",
                callback_data=f"confirm:unfollowall:{user.id}",
            ),
            InlineKeyboardButton("Cancel", callback_data=f"cancel:unfollowall:{user.id}"),
        ]
    ]
    _ = await message.reply_text(
        "⚠️ This will remove ALL your followed topics. Are you sure?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def set_country(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    keyboard = [
        [InlineKeyboardButton(display, callback_data=f"country:{code}")]
        for display, code in COUNTRIES.items()
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    current = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    current_code = current.get("country", DEFAULT_COUNTRY)
    current_name = _country_name_from_code(current_code)

    _ = await message.reply_text(
        f"🌍 Current region: <b>{_escape_html(current_name)}</b>\n\nSelect your news region:",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def set_category(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    keyboard = [
        [InlineKeyboardButton(cat.capitalize(), callback_data=f"category:{cat}")]
        for cat in CATEGORIES
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    current = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    current_cat = current.get("category", "general")

    _ = await message.reply_text(
        f"📂 Current category: <b>{_escape_html(current_cat.capitalize())}</b>\n\n"
        f"Select your news topic:",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def subscribe(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    await increment(COMMAND_TOTAL)
    await increment(COMMAND_SUBSCRIBE)

    if await is_subscriber(chat_id):
        _ = await message.reply_text("You are already subscribed to daily news!")
        return

    await add_subscriber(chat_id)
    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    _ = await message.reply_text(
        f"✅ Subscribed! You'll receive daily news at {DAILY_NEWS_TIME} "
        f"for {prefs['category']} news in {prefs['country'].upper()}.\n"
        f"Use /unsubscribe to stop."
    )


async def unsubscribe(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    await increment(COMMAND_TOTAL)
    await increment(COMMAND_UNSUBSCRIBE)

    if not await is_subscriber(chat_id):
        _ = await message.reply_text("You are not subscribed to daily news.")
        return

    await remove_subscriber(chat_id)
    _ = await message.reply_text("Unsubscribed. You will no longer receive daily news.")


async def preferences(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    country_code = prefs.get("country", DEFAULT_COUNTRY)
    category = prefs.get("category", "general")
    country_name = _country_name_from_code(country_code)
    followed_topics = await get_followed_topics(chat_id)
    breaking_enabled = await get_breaking_news_preference(chat_id)
    breaking_label = "ON" if breaking_enabled else "OFF"
    breaking_emoji = "🔔" if breaking_enabled else "🔕"

    text = (
        "⚙️ <b>Your Preferences</b>\n\n"
        f"🌍 Region: {_escape_html(country_name)}\n"
        f"📂 Category: {_escape_html(category.capitalize())}\n"
        f"🏷️ Followed topics: {len(followed_topics)}\n"
        f"{breaking_emoji} Breaking news: <b>{breaking_label}</b>\n\n"
        "Use /setcountry and /setcategory to change these.\n"
        "Use /follows to see your followed topics.\n"
        "Use /breaking to toggle breaking news alerts."
    )
    _ = await message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def breaking_toggle(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    await increment(COMMAND_TOTAL)
    await increment(COMMAND_BREAKING_TOGGLE)

    current_enabled = await get_breaking_news_preference(chat_id)
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

    await increment(COMMAND_TOTAL)
    await increment(COMMAND_TRENDING)

    raw_category = context.args[0] if context.args else ""
    raw_country = context.args[1] if len(context.args) >= 2 else None

    # Resolve country: explicit override > saved DB preference > DEFAULT_COUNTRY
    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    saved_country = prefs.get("country", DEFAULT_COUNTRY)
    country = raw_country or saved_country or DEFAULT_COUNTRY

    category = _resolve_trending_category(raw_category)

    if category is None:
        supported = ", ".join(sorted(TRENDING_CATEGORY_ALIASES.keys()))
        _ = await message.reply_text(f"⚠️ Unknown trending category.\n\nTry one of: {supported}")
        return

    await _send_trending_results(message, chat_id, category, country)


async def health(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    await increment(COMMAND_TOTAL)
    await increment(COMMAND_HEALTH)

    status_msg = await message.reply_text("🏥 Checking bot health...")

    try:
        api_health = await check_api_health()
        db_health = await check_db_health()
        request_count, request_limit = await get_request_count()

        _ = await status_msg.delete()

        api_emoji = "✅" if api_health["status"] == "healthy" else "❌"
        db_emoji = "✅" if db_health["status"] == "healthy" else "❌"

        health_message = (
            f"🏥 <b>Bot Health Status</b>\n\n{api_emoji} NewsData.io: {api_health['status']}"
        )

        if api_health["status"] == "healthy":
            health_message += f" ({api_health.get('response_time', 'N/A')})"
        else:
            health_message += f"\n   Error: {api_health.get('error', 'Unknown')}"

        health_message += f"\n{db_emoji} Database: {db_health['status']}"

        if db_health["status"] == "healthy":
            health_message += f"\n   Subscribers: {db_health.get('subscriber_count', '0')}"
            health_message += f"\n   Followed topics: {db_health.get('followed_topic_count', '0')}"
            health_message += (
                f"\n   Breaking alerts tracked: {db_health.get('breaking_alert_count', '0')}"
            )
        else:
            health_message += f"\n   Error: {db_health.get('error', 'Unknown')}"

        health_message += f"\n\n📊 API Requests: {request_count}/{request_limit} today"
        health_message += "\n📊 Cache: Active (5min TTL)\n"
        health_message += "🤖 Bot: Running"

        metrics = await all_metrics()
        health_message += "\n\n📈 <b>Metrics</b>"
        health_message += f"\n  Commands: {metrics.get(COMMAND_TOTAL, 0)}"
        health_message += f"\n  /news: {metrics.get(COMMAND_NEWS, 0)}"
        health_message += f"\n  /search: {metrics.get(COMMAND_SEARCH, 0)}"
        health_message += f"\n  News API errors: {metrics.get(NEWS_API_ERRORS, 0)}"

        _ = await message.reply_text(health_message, parse_mode=ParseMode.HTML)

    except Exception as exc:
        logger.error("Error checking health: %s", exc)
        _ = await status_msg.edit_text("🔧 Failed to check health status.")


async def clear_chat(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/clear`` — ask for confirmation, then delete recent messages.

    The actual deletion is performed by the ``button_handler`` branch for
    ``confirm:clear:<user_id>:<orig_msg_id>``, which calls
    :func:`_clear_chat_messages` starting from the original ``/clear``
    message id. Encoding the id in the callback keeps the existing
    60-message-back behavior intact: we delete the 60 messages ending at the
    ``/clear`` command itself, not the confirmation prompt.
    """
    message = update.effective_message
    user = update.effective_user
    chat_id = _effective_chat_id(update)
    if not message or not user or chat_id is None:
        return

    keyboard = [
        [
            InlineKeyboardButton(
                "Yes, clear recent messages",
                callback_data=f"confirm:clear:{user.id}:{message.message_id}",
            ),
            InlineKeyboardButton("Cancel", callback_data=f"cancel:clear:{user.id}"),
        ]
    ]
    _ = await message.reply_text(
        "⚠️ This will delete the last ~60 messages in this chat, including "
        "this command. Are you sure?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
