import logging
from datetime import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram import BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

from config import (
    TELEGRAM_BOT_TOKEN,
    DAILY_NEWS_TIME,
    DEFAULT_COUNTRY,
    COUNTRIES,
    CATEGORIES,
    BREAKING_NEWS_KEYWORDS,
    BREAKING_CHECK_INTERVAL_MINUTES,
    BREAKING_RATE_LIMIT_HOURS,
)
from news_fetcher import (
    fetch_top_headlines,
    format_briefing,
    search_news,
    format_search_results,
    get_article_image,
    APIClientError,
    fetch_breaking_news,
    fetch_trending_topics,
    check_api_health,
    get_request_count,
)
from database import (
    load_subscribers,
    add_subscriber,
    remove_subscriber,
    is_subscriber,
    get_user_prefs,
    set_user_prefs,
    get_breaking_news_preference,
    set_breaking_news_preference,
    check_db_health,
)
from message_utils import send_chunked_message, chunk_message

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Rate limiting for search command: {chat_id: last_search_timestamp}
_search_rate_limit: dict[int, float] = {}
SEARCH_COOLDOWN_SECONDS = 10

# Rate limiting for breaking news: {chat_id: last_sent_timestamp}
_breaking_rate_limit: dict[int, float] = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome = (
        "Welcome to Daily News Bot! 📰\n\n"
        "Use /news to get today's news briefing.\n"
        "Use /subscribe to receive daily news automatically.\n"
        "Use /search <topic> to search for specific news.\n"
        "Use /setcountry to pick your region.\n"
        "Use /setcategory to choose topics.\n"
        "Use /help to see all commands."
    )
    await update.message.reply_text(welcome)


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = get_user_prefs(update.effective_chat.id, DEFAULT_COUNTRY)
    country = prefs.get("country", DEFAULT_COUNTRY)
    category = prefs.get("category", "general")

    status_msg = await update.message.reply_text("Fetching latest news...")

    try:
        data = await fetch_top_headlines(country, category)
        articles = data.get("articles", [])

        if not articles:
            await status_msg.edit_text(
                f"No {category} news articles found for {country.upper()}. Try again later."
            )
            return

        await status_msg.delete()

        cat_label = category.capitalize() if category != "general" else "Top"
        published_at = articles[0].get("publishedAt", "")
        date_str = published_at[:10] if published_at else ""
        header = (
            f"📰 <b>Daily News Briefing — {date_str}</b>\n"
            f"🌍 {cat_label} Headlines ({country.upper()})\n"
        )
        await update.message.reply_text(header, parse_mode=ParseMode.HTML)

        for i, article in enumerate(articles[:10], 1):
            title = article.get("title", "No title")
            url = article.get("url", "")
            description = article.get("description", "")
            source = article.get("source", {}).get("name", "Unknown")
            image_url = get_article_image(article)

            caption = (
                f"<b>{i}. {title}</b>\n"
                f"<i>{(description[:150] + '...') if description and len(description) > 150 else description}</i>\n"
                f"📍 {source}"
            )

            keyboard = (
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📖 Read full article", url=url)]]
                )
                if url
                else None
            )

            try:
                if image_url:
                    await update.message.reply_photo(
                        photo=image_url,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
                else:
                    await update.message.reply_text(
                        caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
            except Exception:
                await update.message.reply_text(
                    caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )

        await update.message.reply_text("Stay informed! 🌍")

    except APIClientError as e:
        logger.error("News API error fetching news: %s", e)
        await status_msg.edit_text(str(e))
    except Exception as e:
        logger.error("Unexpected error fetching news: %s", e)
        await status_msg.edit_text(
            "🔧 An unexpected error occurred. Please try again later."
        )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: /search <topic>\nExample: /search bitcoin"
        )
        return

    chat_id = update.effective_chat.id
    current_time = context._application._loop.time()

    # Check rate limit
    last_search = _search_rate_limit.get(chat_id, 0)
    if current_time - last_search < SEARCH_COOLDOWN_SECONDS:
        remaining = int(SEARCH_COOLDOWN_SECONDS - (current_time - last_search))
        await update.message.reply_text(
            f"⏳ Please wait {remaining} second(s) before searching again."
        )
        return

    query = " ".join(context.args)
    status_msg = await update.message.reply_text(f'🔍 Searching for "{query}"...')

    try:
        data = await search_news(query)
        results = format_search_results(data, query)
        await status_msg.delete()
        await send_chunked_message(
            update.message, results, parse_mode="HTML", disable_web_page_preview=True
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


async def set_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = []
    for display, code in COUNTRIES.items():
        keyboard.append(
            [InlineKeyboardButton(display, callback_data=f"country:{code}")]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)
    current = get_user_prefs(update.effective_chat.id, DEFAULT_COUNTRY)
    current_code = current.get("country", DEFAULT_COUNTRY)
    current_name = next(
        (k for k, v in COUNTRIES.items() if v == current_code), current_code
    )

    await update.message.reply_text(
        f"🌍 Current region: <b>{current_name}</b>\n\nSelect your news region:",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def set_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = []
    for cat in CATEGORIES:
        keyboard.append(
            [InlineKeyboardButton(cat.capitalize(), callback_data=f"category:{cat}")]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)
    current = get_user_prefs(update.effective_chat.id, DEFAULT_COUNTRY)
    current_cat = current.get("category", "general")

    await update.message.reply_text(
        f"📂 Current category: <b>{current_cat.capitalize()}</b>\n\nSelect your news topic:",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = query.message.chat_id

    if data.startswith("country:"):
        parts = data.split(":")
        if len(parts) < 2:
            logger.warning(f"Invalid callback data format: {data}")
            return
        code = parts[1]
        name = next((k for k, v in COUNTRIES.items() if v == code), code)
        set_user_prefs(chat_id, country=code)
        await query.edit_message_text(
            f"✅ Region set to <b>{name}</b>", parse_mode="HTML"
        )

    elif data.startswith("category:"):
        parts = data.split(":")
        if len(parts) < 2:
            logger.warning(f"Invalid callback data format: {data}")
            return
        cat = parts[1]
        set_user_prefs(chat_id, category=cat)
        await query.edit_message_text(
            f"✅ Category set to <b>{cat.capitalize()}</b>", parse_mode="HTML"
        )

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
    chat_id = update.effective_chat.id
    if is_subscriber(chat_id):
        await update.message.reply_text("You are already subscribed to daily news!")
        return

    add_subscriber(chat_id)
    prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
    await update.message.reply_text(
        f"✅ Subscribed! You'll receive daily news at {DAILY_NEWS_TIME} "
        f"for {prefs['category']} news in {prefs['country'].upper()}.\n"
        f"Use /unsubscribe to stop."
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_subscriber(chat_id):
        await update.message.reply_text("You are not subscribed to daily news.")
        return

    remove_subscriber(chat_id)
    await update.message.reply_text(
        "Unsubscribed. You will no longer receive daily news."
    )


async def preferences(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = get_user_prefs(update.effective_chat.id, DEFAULT_COUNTRY)
    country_code = prefs.get("country", DEFAULT_COUNTRY)
    category = prefs.get("category", "general")

    country_name = next(
        (k for k, v in COUNTRIES.items() if v == country_code), country_code
    )

    text = (
        f"⚙️ <b>Your Preferences</b>\n\n"
        f"🌍 Region: {country_name}\n"
        f"📂 Category: {category.capitalize()}\n\n"
        f"Use /setcountry and /setcategory to change these."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await update.message.reply_text(help_text, parse_mode="HTML")


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
        request_count, request_limit = get_request_count()
        
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
        
        message += f"\n\n📊 API Requests: {request_count}/{request_limit} today"
        message += f"\n📊 Cache: Active (5min TTL)\n"
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

    logger.info(f"Sending daily news to {len(subscribers)} subscribers...")

    for chat_id in subscribers:
        try:
            prefs = get_user_prefs(chat_id, DEFAULT_COUNTRY)
            country = prefs.get("country", DEFAULT_COUNTRY)
            category = prefs.get("category", "general")

            data = await fetch_top_headlines(country, category)
            articles = data.get("articles", [])

            if not articles:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"No {category} news articles found for {country.upper()}.",
                )
                continue

            cat_label = category.capitalize() if category != "general" else "Top"
            published_at = articles[0].get("publishedAt", "")
            date_str = published_at[:10] if published_at else ""
            header = (
                f"📰 <b>Daily News Briefing — {date_str}</b>\n"
                f"🌍 {cat_label} Headlines ({country.upper()})\n"
            )
            await context.bot.send_message(
                chat_id=chat_id, text=header, parse_mode=ParseMode.HTML
            )

            for i, article in enumerate(articles[:10], 1):
                title = article.get("title", "No title")
                url = article.get("url", "")
                description = article.get("description", "")
                source = article.get("source", {}).get("name", "Unknown")
                image_url = get_article_image(article)

                caption = (
                    f"<b>{i}. {title}</b>\n"
                    f"<i>{(description[:150] + '...') if description and len(description) > 150 else description}</i>\n"
                    f"📍 {source}"
                )

                keyboard = (
                    InlineKeyboardMarkup(
                        [[InlineKeyboardButton("📖 Read full article", url=url)]]
                    )
                    if url
                    else None
                )

                try:
                    if image_url:
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=image_url,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=keyboard,
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=keyboard,
                        )
                except Exception:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )

            await context.bot.send_message(chat_id=chat_id, text="Stay informed! 🌍")

        except APIClientError as e:
            logger.error("News API error sending daily news to %s: %s", chat_id, e)
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=f"⚠️ Error fetching today's news: {e}"
                )
            except Exception:
                pass
        except Exception as e:
            logger.error("Failed to send news to %s: %s", chat_id, e)


async def send_breaking_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check for breaking news and send to opted-in users."""
    subscribers = load_subscribers()
    if not subscribers:
        logger.info("No subscribers to check for breaking news.")
        return

    # Filter users who have breaking news enabled
    breaking_enabled_users = []
    for chat_id in subscribers:
        if get_breaking_news_preference(chat_id):
            breaking_enabled_users.append(chat_id)
    
    if not breaking_enabled_users:
        logger.info("No users with breaking news enabled.")
        return

    logger.info(f"Checking breaking news for {len(breaking_enabled_users)} users...")

    try:
        countries = list(COUNTRIES.values())
        breaking_articles = await fetch_breaking_news(countries, BREAKING_NEWS_KEYWORDS)
        
        if not breaking_articles:
            logger.info("No breaking news found.")
            return

        # Deduplicate articles by URL
        seen_urls = set()
        unique_articles = []
        for article in breaking_articles:
            url = article.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_articles.append(article)
        
        if not unique_articles:
            logger.info("No unique breaking news articles.")
            return

        logger.info(f"Found {len(unique_articles)} breaking news articles.")

        for chat_id in breaking_enabled_users:
            # Check rate limit
            current_time = context._application._loop.time()
            last_sent = _breaking_rate_limit.get(chat_id, 0)
            rate_limit_seconds = BREAKING_RATE_LIMIT_HOURS * 3600
            
            if current_time - last_sent < rate_limit_seconds:
                continue

            try:
                for article in unique_articles[:3]:  # Send max 3 breaking articles
                    title = article.get("title", "No title")
                    url = article.get("url", "")
                    description = article.get("description", "")
                    source = article.get("source", {}).get("name", "Unknown")
                    country = article.get("country", "us").upper()
                    image_url = get_article_image(article)

                    caption = (
                        f"🚨 <b>BREAKING NEWS</b> ({country})\n\n"
                        f"<b>{title}</b>\n"
                        f"<i>{(description[:150] + '...') if description and len(description) > 150 else description}</i>\n"
                        f"📍 {source}"
                    )

                    keyboard = (
                        InlineKeyboardMarkup(
                            [[InlineKeyboardButton("📖 Read full article", url=url)]]
                        )
                        if url
                        else None
                    )

                    try:
                        if image_url:
                            await context.bot.send_photo(
                                chat_id=chat_id,
                                photo=image_url,
                                caption=caption,
                                parse_mode=ParseMode.HTML,
                                reply_markup=keyboard,
                            )
                        else:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=caption,
                                parse_mode=ParseMode.HTML,
                                reply_markup=keyboard,
                            )
                    except Exception:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=keyboard,
                        )

                # Update rate limit
                _breaking_rate_limit[chat_id] = current_time
                logger.info("Sent breaking news to %s", chat_id)

            except Exception as e:
                logger.error("Failed to send breaking news to %s: %s", chat_id, e)

    except Exception as e:
        logger.error("Error fetching breaking news: %s", e)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables")
        return

    hour, minute = map(int, DAILY_NEWS_TIME.split(":"))
    daily_time = time(hour, minute)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("setcountry", set_country))
    app.add_handler(CommandHandler("setcategory", set_category))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("prefs", preferences))
    app.add_handler(CommandHandler("breaking", breaking_toggle))
    app.add_handler(CommandHandler("trending", trending))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Register commands so Telegram shows suggestions when user types "/"
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

    async def setup_commands(application):
        await application.bot.set_my_commands(commands)

    app.post_init = setup_commands

    app.job_queue.run_daily(send_daily_news, time=daily_time)
    logger.info(f"Daily news scheduled for {DAILY_NEWS_TIME}")

    # Schedule breaking news check every 30 minutes
    app.job_queue.run_repeating(
        send_breaking_news,
        interval=60 * BREAKING_CHECK_INTERVAL_MINUTES,
        first=10,  # Start 10 seconds after bot starts
    )
    logger.info(f"Breaking news check scheduled every {BREAKING_CHECK_INTERVAL_MINUTES} minutes")

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
