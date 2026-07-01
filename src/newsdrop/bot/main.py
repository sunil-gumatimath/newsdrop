from __future__ import annotations

import asyncio
import signal
import sys
import threading
from types import FrameType
from typing import Any, cast

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from ..config import (
    BREAKING_ALERT_INTERVAL_MINUTES,
    DAILY_NEWS_TIME,
    TELEGRAM_BOT_TOKEN,
)
from ..logging_config import setup_logging
from ..metrics import UNEXPECTED_ERRORS, increment
from .callbacks import button_handler
from .commands import (
    breaking_toggle,
    clear_chat,
    follow_topic,
    health,
    help_command,
    list_followed_topics,
    news,
    preferences,
    search,
    set_category,
    set_country,
    start,
    subscribe,
    trending,
    unfollow_all_topics,
    unfollow_topic,
    unsubscribe,
)
from .health_server import set_ready, start_health_server
from .helpers import (
    _parse_daily_time,
    logger,
)
from .jobs import (
    send_breaking_news_alerts,
    send_daily_news,
)


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


async def error_handler(update: Update | object, context: ContextTypes.DEFAULT_TYPE) -> None:
    await increment(UNEXPECTED_ERRORS)
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


async def _drain_and_stop(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
    """Graceful shutdown: stop accepting new updates, drain the job queue, then exit."""
    logger.info("Draining in-flight updates...")
    try:
        await app.stop()
    except Exception:
        logger.exception("Error while stopping the application")

    logger.info("Shutting down job queue...")
    try:
        if app.job_queue is not None:
            app.job_queue.scheduler.shutdown(wait=False)
    except Exception:
        logger.exception("Error while shutting down job queue")

    try:
        await app.shutdown()
    except Exception:
        logger.exception("Error during application shutdown")


def main() -> None:
    setup_logging()

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables")
        sys.exit(1)

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

    # Start HTTP health server for Docker/container orchestration.
    start_health_server()

    # Mark the application as ready so /ready returns 200.
    set_ready(True)

    # Graceful shutdown: drain in-flight updates and stop the job queue
    # when SIGTERM/SIGINT arrives. The signal handler flips _ready and
    # schedules shutdown_event.set() on the main loop; after run_polling
    # returns, we drain the application.
    shutdown_event = threading.Event()

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — initiating graceful shutdown...", sig_name)
        set_ready(False)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("Daily news scheduled for %s", DAILY_NEWS_TIME)
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

    # After polling stops (e.g. via shutdown signal), drain + clean up.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_drain_and_stop(app))
    finally:
        loop.close()
    logger.info("Bot shut down cleanly.")
