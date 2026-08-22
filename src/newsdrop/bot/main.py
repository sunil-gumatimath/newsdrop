from __future__ import annotations

import asyncio
import sys
from typing import Any, cast

from apscheduler.schedulers import SchedulerNotRunningError
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from ..config import (
    BREAKING_ALERT_INTERVAL_MINUTES,
    TELEGRAM_BOT_TOKEN,
    validate_config,
)
from ..logging_config import setup_logging
from ..metrics import UNEXPECTED_ERRORS, increment
from ..news_fetcher import close_http_client
from .callbacks import button_handler
from .commands import (
    breaking_toggle,
    breakkeywords,
    clear_chat,
    follow_topic,
    health,
    help_command,
    list_followed_topics,
    news,
    preferences,
    quiet_hours,
    search,
    set_category,
    set_country,
    set_time,
    set_timezone,
    start,
    subscribe,
    trending,
    unfollow_all_topics,
    unfollow_topic,
    unsubscribe,
)
from .health_server import set_ready, start_health_server
from .helpers import logger
from .jobs import (
    send_breaking_news_alerts,
    send_daily_news,
)


async def _setup_commands(
    application: Application[Any, Any, Any, Any, Any, Any],
) -> None:
    # Keep the menu short; full list lives in /help.
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("news", "Get latest news briefing"),
        BotCommand("subscribe", "Enable daily news delivery"),
        BotCommand("search", "Search news by topic"),
        BotCommand("follow", "Follow a topic"),
        BotCommand("setcountry", "Choose news region"),
        BotCommand("setcategory", "Choose news category"),
        BotCommand("settime", "Set daily digest hour"),
        BotCommand("settimezone", "Set your timezone"),
        BotCommand("breaking", "Breaking news alerts"),
        BotCommand("prefs", "View preferences"),
        BotCommand("clear", "Clear recent bot messages"),
        BotCommand("help", "Show all commands"),
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
    """Graceful shutdown: drain in-flight updates and stop the job queue.

    ``run_polling`` already calls ``app.stop()`` and ``app.shutdown()``
    internally when the shutdown event fires, so we only need to tear
    down the job-queue scheduler here.
    """
    logger.debug("Draining in-flight updates...")
    try:
        if app.job_queue is not None:
            app.job_queue.scheduler.shutdown(wait=False)
    except SchedulerNotRunningError:
        logger.debug("Scheduler was already stopped — nothing to shut down.")
    except Exception:
        logger.exception("Error while shutting down job queue")
    try:
        await close_http_client()
    except Exception:
        logger.exception("Error while closing HTTP client")


async def _post_shutdown(_app: Application) -> None:
    """Flip readiness off once PTB has fully shut down (run_polling calls this)."""
    set_ready(False)
    logger.info("Application shutdown complete — readiness set to False.")


def main() -> None:
    setup_logging()

    # Fail fast on missing/malformed configuration before touching Telegram.
    validate_config()

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables")
        sys.exit(1)

    # Initialize the database schema before handlers/jobs run.
    from ..database import _init_db  # local import keeps module import side-effect-free

    _init_db()

    # post_init must be set via the builder — ``Application.post_init`` is a
    # read-only property in PTB v20+ and assigning it raises AttributeError.
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_setup_commands)
        .post_shutdown(_post_shutdown)
        .build()
    )
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
    app.add_handler(CommandHandler("settime", set_time))
    app.add_handler(CommandHandler("settimezone", set_timezone))
    app.add_handler(CommandHandler("quiet", quiet_hours))
    app.add_handler(CommandHandler("breakkeywords", breakkeywords))
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

    if app.job_queue is None:
        logger.error("Job queue is unavailable. Install job-queue dependencies.")
        return

    # Hourly tick: each subscriber is due when local hour == their preferred hour.
    app.job_queue.run_repeating(
        send_daily_news,
        interval=3600,
        first=30,
        name="daily_news_hourly",
    )

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

    # Signal handling is left to run_polling: it installs its own SIGINT/SIGTERM
    # handlers (loop.add_signal_handler on POSIX) that stop the loop cleanly.
    # Custom handlers here would either be replaced (POSIX) or swallow Ctrl+C
    # so polling never stops (Windows). Readiness flip happens in post_shutdown.

    logger.info("Daily news job runs hourly; delivery uses each user's timezone/hour")
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

    # After polling stops (e.g. via shutdown signal), drain + clean up.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_drain_and_stop(app))
    finally:
        loop.close()
    logger.info("Bot shut down cleanly.")
