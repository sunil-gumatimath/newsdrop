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
)
from news_fetcher import (
    fetch_top_headlines,
    format_briefing,
    search_news,
    format_search_results,
    get_article_image,
    APIClientError,
)
from database import (
    load_subscribers,
    add_subscriber,
    remove_subscriber,
    is_subscriber,
    get_user_prefs,
    set_user_prefs,
)
from message_utils import send_chunked_message, chunk_message

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Rate limiting for search command: {chat_id: last_search_timestamp}
_search_rate_limit: dict[int, float] = {}
SEARCH_COOLDOWN_SECONDS = 10


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
        "/help - Show this help message"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")


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
        BotCommand("help", "Show all commands"),
    ]

    async def setup_commands(application):
        await application.bot.set_my_commands(commands)

    app.post_init = setup_commands

    app.job_queue.run_daily(send_daily_news, time=daily_time)
    logger.info(f"Daily news scheduled for {DAILY_NEWS_TIME}")

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
