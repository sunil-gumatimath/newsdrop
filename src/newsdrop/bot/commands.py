from __future__ import annotations

import contextlib

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..config import (
    BREAKING_ALERT_INTERVAL_MINUTES,
    BREAKING_ALERT_MAX_PER_DAY,
    CATEGORIES,
    COMMON_TIMEZONES,
    COUNTRIES,
    DAILY_HOUR_CHOICES,
    DEFAULT_COUNTRY,
    DEFAULT_DAILY_HOUR,
    DEFAULT_TIMEZONE,
    MAX_BREAKING_KEYWORDS_PER_USER,
)
from ..database import (
    add_followed_topic,
    add_subscriber,
    check_db_health,
    get_breaking_news_preference,
    get_followed_topics,
    get_user_prefs,
    is_subscriber,
    parse_breaking_keywords,
    remove_followed_topic,
    remove_subscriber,
    serialize_breaking_keywords,
    set_user_prefs,
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
    TRENDING_COOLDOWN_SECONDS,
    TRENDING_RATE_LIMIT_SCOPE,
    Prefs,
    _build_digest_payload,
    _country_name_from_code,
    _effective_chat_id,
    _escape_html,
    _format_followed_topics,
    _resolve_trending_category,
    _sanitize_follow_topic,
    _send_trending_results,
    build_search_payload,
    is_admin_chat,
    logger,
)


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    await increment(COMMAND_TOTAL)

    from .helpers import country_keyboard

    welcome = (
        "Welcome to <b>newsdrop</b> 📰\n\n"
        "Personalized headlines in Telegram — multi-source, ranked, short blurbs.\n\n"
        "<b>Step 1 of 3 — pick your region</b>\n"
        "Then choose a category and (optionally) subscribe to a daily briefing.\n\n"
        "You can also jump ahead: /news · /help"
    )
    _ = await message.reply_text(
        welcome,
        parse_mode=ParseMode.HTML,
        reply_markup=country_keyboard(onboarding=True),
    )


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
        unit = "second" if NEWS_COOLDOWN_SECONDS == 1 else "seconds"
        _ = await message.reply_text(
            f"⏳ You're on a cooldown. Try again in {NEWS_COOLDOWN_SECONDS} {unit}. "
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
        result = _build_digest_payload(data, category, country, followed)

        if result.empty_message:
            _ = await status_msg.edit_text(
                result.empty_message,
                parse_mode=ParseMode.HTML,
                reply_markup=result.reply_markup,
            )
            return

        if result.digest is None:
            return

        if len(result.digest) <= 4096:
            await status_msg.edit_text(
                result.digest,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=result.reply_markup,
            )
        else:
            with contextlib.suppress(Exception):
                await status_msg.delete()
            await send_chunked_message(
                message,
                result.digest,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            # Attach open-article buttons on a short follow-up when chunked.
            if result.reply_markup is not None:
                with contextlib.suppress(Exception):
                    _ = await message.reply_text(
                        "📖 Open full articles:",
                        reply_markup=result.reply_markup,
                    )

        # Record the successful call for cooldown tracking.
        await rate_limit_record(NEWS_RATE_LIMIT_SCOPE, chat_id, NEWS_COOLDOWN_SECONDS)

    except APIClientError:
        await increment(NEWS_API_ERRORS)
        logger.exception("Failed to fetch news")
        _ = await status_msg.edit_text("🔧 Could not fetch news. Please try again later.")
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
        unit = "second" if SEARCH_COOLDOWN_SECONDS == 1 else "seconds"
        _ = await message.reply_text(
            f"⏳ Please wait {SEARCH_COOLDOWN_SECONDS} {unit} before searching again."
        )
        return

    query = " ".join(context.args).strip()
    if not query:
        _ = await message.reply_text("Usage: /search <topic>\nExample: /search bitcoin")
        return

    if len(query) > 200:
        _ = await message.reply_text("⚠️ Query too long (max 200 characters).")
        return

    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    country = prefs.get("country", DEFAULT_COUNTRY)

    status_msg = await message.reply_text(f'🔍 Searching for "{query}"...')

    try:
        data = await search_news(query, country)
        result = build_search_payload(data, query)

        if result.empty_message:
            _ = await status_msg.edit_text(
                result.empty_message,
                parse_mode=ParseMode.HTML,
                reply_markup=result.reply_markup,
            )
            await rate_limit_record(SEARCH_RATE_LIMIT_SCOPE, chat_id, SEARCH_COOLDOWN_SECONDS)
            return

        if result.digest is None:
            return

        if len(result.digest) <= 4096:
            _ = await status_msg.edit_text(
                result.digest,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=result.reply_markup,
            )
        else:
            with contextlib.suppress(Exception):
                await status_msg.delete()
            await send_chunked_message(
                message,
                result.digest,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            if result.reply_markup is not None:
                with contextlib.suppress(Exception):
                    _ = await message.reply_text(
                        "📖 Open results / follow topic:",
                        reply_markup=result.reply_markup,
                    )
        await rate_limit_record(SEARCH_RATE_LIMIT_SCOPE, chat_id, SEARCH_COOLDOWN_SECONDS)
    except APIClientError:
        await increment(NEWS_API_ERRORS)
        logger.exception("Failed to search news")
        _ = await status_msg.edit_text("🔧 Could not fetch news. Please try again later.")
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

    from .helpers import country_keyboard

    current = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    current_code = current.get("country", DEFAULT_COUNTRY)
    current_name = _country_name_from_code(current_code)

    _ = await message.reply_text(
        f"🌍 Current region: <b>{_escape_html(current_name)}</b>\n\nSelect your news region:",
        parse_mode=ParseMode.HTML,
        reply_markup=country_keyboard(onboarding=False),
    )


async def set_category(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    from .helpers import category_keyboard

    current = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    current_cat = current.get("category", "general")

    _ = await message.reply_text(
        f"📂 Current category: <b>{_escape_html(current_cat.capitalize())}</b>\n\n"
        f"Select your news topic:",
        parse_mode=ParseMode.HTML,
        reply_markup=category_keyboard(onboarding=False),
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
    hour = prefs.get("daily_hour", str(DEFAULT_DAILY_HOUR))
    tz = prefs.get("timezone", DEFAULT_TIMEZONE)
    _ = await message.reply_text(
        f"✅ Subscribed! You'll receive daily news around "
        f"<b>{_escape_html(hour)}:00</b> ({_escape_html(tz)}) "
        f"for {_escape_html(prefs['category'])} · "
        f"{_escape_html(prefs['country'].upper())}.\n"
        "Use /settime and /settimezone to change delivery. /unsubscribe to stop.",
        parse_mode=ParseMode.HTML,
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
    hour = prefs.get("daily_hour", str(DEFAULT_DAILY_HOUR))
    tz = prefs.get("timezone", DEFAULT_TIMEZONE)
    use_follows = prefs.get("breaking_use_follows", "1") != "0"
    custom_kw = parse_breaking_keywords(prefs.get("breaking_keywords", ""))
    quiet_start = prefs.get("quiet_start_hour", "")
    quiet_end = prefs.get("quiet_end_hour", "")
    if quiet_start != "" and quiet_end != "":
        quiet_label = f"{quiet_start}:00–{quiet_end}:00"
    else:
        quiet_label = "off"

    text = (
        "⚙️ <b>Your Preferences</b>\n\n"
        f"🌍 Region: {_escape_html(country_name)}\n"
        f"📂 Category: {_escape_html(category.capitalize())}\n"
        f"🕒 Digest: {_escape_html(hour)}:00 ({_escape_html(tz)})\n"
        f"🌙 Quiet hours: {_escape_html(quiet_label)}\n"
        f"🏷️ Followed topics: {len(followed_topics)}\n"
        f"{breaking_emoji} Breaking news: <b>{breaking_label}</b>\n"
        f"   · Followed topics as alerts: {'ON' if use_follows else 'OFF'}\n"
        f"   · Custom keywords: {len(custom_kw)}"
        f"{(' — ' + _escape_html(', '.join(custom_kw))) if custom_kw else ''}\n"
        f"   · Max {BREAKING_ALERT_MAX_PER_DAY}/day\n\n"
        "/setcountry · /setcategory · /settime · /settimezone\n"
        "/quiet · /breaking · /breakkeywords · /follows"
    )
    _ = await message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        logger.warning("Help command received without an effective message")
        return

    help_text = (
        "<b>newsdrop commands</b>\n\n"
        "<b>Daily</b>\n"
        "/news — briefing now\n"
        "/subscribe · /unsubscribe — scheduled digests\n"
        "/settime · /settimezone — delivery schedule\n"
        "/setcountry · /setcategory · /prefs\n\n"
        "<b>Discover</b>\n"
        "/search &lt;topic&gt;\n"
        "/trending [category]\n"
        "/follow · /unfollow · /follows · /unfollowall\n\n"
        "<b>Alerts</b>\n"
        "/breaking — on/off + alert sources\n"
        "/breakkeywords — personal alert keywords\n"
        "/quiet — mute alerts overnight\n\n"
        "<b>Utilities</b>\n"
        "/clear — clear recent chat (bot messages; confirm first)\n"
        "/help · /commands\n"
        "/health — admin only (ops diagnostics)"
    )

    try:
        _ = await message.reply_text(help_text, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Failed to send /help response")
        _ = await message.reply_text(
            "newsdrop: /news /subscribe /setcountry /setcategory "
            "/settime /settimezone /search /follow /breaking "
            "/breakkeywords /quiet /prefs /clear /help"
        )


async def set_time(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick preferred local hour for the daily digest."""
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    current = prefs.get("daily_hour", str(DEFAULT_DAILY_HOUR))
    keyboard = [
        [
            InlineKeyboardButton(
                f"{h:02d}:00" + (" ✓" if str(h) == str(current) else ""),
                callback_data=f"dailyhour:{h}",
            )
            for h in DAILY_HOUR_CHOICES[i : i + 4]
        ]
        for i in range(0, len(DAILY_HOUR_CHOICES), 4)
    ]
    _ = await message.reply_text(
        f"🕒 Current digest hour: <b>{_escape_html(current)}:00</b> "
        f"({_escape_html(prefs.get('timezone', DEFAULT_TIMEZONE))})\n\n"
        "Pick a local hour for your daily briefing:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set IANA timezone for digest timing and quiet hours."""
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    args = context.args or []
    if args:
        tz_name = args[0].strip()
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(tz_name)
        except Exception:
            _ = await message.reply_text(
                "⚠️ Unknown timezone. Use an IANA name like "
                "<code>America/New_York</code> or pick a button below.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await set_user_prefs(chat_id, timezone=tz_name)
            _ = await message.reply_text(
                f"✅ Timezone set to <b>{_escape_html(tz_name)}</b>",
                parse_mode=ParseMode.HTML,
            )
            return

    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    current = prefs.get("timezone", DEFAULT_TIMEZONE)
    keyboard = [
        [InlineKeyboardButton(tz + (" ✓" if tz == current else ""), callback_data=f"tz:{tz}")]
        for tz in COMMON_TIMEZONES
    ]
    _ = await message.reply_text(
        f"🌐 Current timezone: <b>{_escape_html(current)}</b>\n\n"
        "Pick a timezone, or send "
        "<code>/settimezone America/New_York</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def quiet_hours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Configure quiet hours for breaking alerts: /quiet 22 7 or /quiet off."""
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    args = [a.lower() for a in (context.args or [])]
    if not args or args[0] in {"help", "?"}:
        prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
        qs, qe = prefs.get("quiet_start_hour", ""), prefs.get("quiet_end_hour", "")
        current = f"{qs}:00–{qe}:00" if qs != "" and qe != "" else "off"
        _ = await message.reply_text(
            f"🌙 Quiet hours (local): <b>{_escape_html(current)}</b>\n\n"
            "Usage:\n"
            "<code>/quiet 22 7</code> — mute alerts 22:00–07:00\n"
            "<code>/quiet off</code> — disable quiet hours",
            parse_mode=ParseMode.HTML,
        )
        return

    if args[0] in {"off", "clear", "none", "disable"}:
        await set_user_prefs(chat_id, clear_quiet_hours=True)
        _ = await message.reply_text("✅ Quiet hours disabled.")
        return

    if len(args) < 2:
        _ = await message.reply_text("⚠️ Need start and end hours, e.g. /quiet 22 7")
        return

    try:
        start = int(args[0])
        end = int(args[1])
    except ValueError:
        _ = await message.reply_text("⚠️ Hours must be integers 0–23.")
        return

    if not (0 <= start <= 23 and 0 <= end <= 23):
        _ = await message.reply_text("⚠️ Hours must be between 0 and 23.")
        return
    if start == end:
        _ = await message.reply_text("⚠️ Start and end must differ (or use /quiet off).")
        return

    await set_user_prefs(chat_id, quiet_start_hour=start, quiet_end_hour=end)
    _ = await message.reply_text(
        f"✅ Quiet hours set to <b>{start}:00–{end}:00</b> (your timezone).",
        parse_mode=ParseMode.HTML,
    )


async def breakkeywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manage personal breaking-alert keywords."""
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    args = context.args or []
    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    current = parse_breaking_keywords(prefs.get("breaking_keywords", ""))

    if not args:
        listed = ", ".join(current) if current else "(none — using follows and/or defaults)"
        _ = await message.reply_text(
            "🔑 <b>Breaking keywords</b>\n\n"
            f"Current: {_escape_html(listed)}\n\n"
            "Usage:\n"
            "<code>/breakkeywords add earthquake</code>\n"
            "<code>/breakkeywords remove earthquake</code>\n"
            "<code>/breakkeywords clear</code>\n"
            f"Limit: {MAX_BREAKING_KEYWORDS_PER_USER} keywords.",
            parse_mode=ParseMode.HTML,
        )
        return

    action = args[0].lower()
    rest = " ".join(args[1:]).strip()

    if action == "clear":
        await set_user_prefs(chat_id, breaking_keywords="")
        _ = await message.reply_text("✅ Cleared custom breaking keywords.")
        return

    if action in {"add", "remove", "rm", "del"} and not rest:
        _ = await message.reply_text("⚠️ Provide a keyword after the action.")
        return

    if action == "add":
        cleaned = _sanitize_follow_topic(rest)
        if len(cleaned) < 3:
            _ = await message.reply_text("⚠️ Keyword must be at least 3 characters.")
            return
        existing = {k.lower(): k for k in current}
        if cleaned.lower() in existing:
            _ = await message.reply_text("You already have that keyword.")
            return
        if len(current) >= MAX_BREAKING_KEYWORDS_PER_USER:
            _ = await message.reply_text(
                f"⚠️ Max {MAX_BREAKING_KEYWORDS_PER_USER} keywords. Remove one first."
            )
            return
        current.append(cleaned)
        await set_user_prefs(chat_id, breaking_keywords=serialize_breaking_keywords(current))
        _ = await message.reply_text(
            f"✅ Added keyword <b>{_escape_html(cleaned)}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if action in {"remove", "rm", "del"}:
        target = rest.lower()
        new_list = [k for k in current if k.lower() != target]
        if len(new_list) == len(current):
            _ = await message.reply_text("⚠️ That keyword is not in your list.")
            return
        await set_user_prefs(chat_id, breaking_keywords=serialize_breaking_keywords(new_list))
        _ = await message.reply_text(
            f"✅ Removed keyword <b>{_escape_html(rest)}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    _ = await message.reply_text(
        "⚠️ Unknown action. Use add / remove / clear.\n"
        "Example: <code>/breakkeywords add climate</code>",
        parse_mode=ParseMode.HTML,
    )


async def breaking_toggle(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = _effective_chat_id(update)
    if not message or chat_id is None:
        return

    await increment(COMMAND_TOTAL)
    await increment(COMMAND_BREAKING_TOGGLE)

    current_enabled = await get_breaking_news_preference(chat_id)
    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    use_follows = prefs.get("breaking_use_follows", "1") != "0"
    custom = parse_breaking_keywords(prefs.get("breaking_keywords", ""))

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔔 Turn ON" if not current_enabled else "🔕 Turn OFF",
                    callback_data=f"breaking:{1 if not current_enabled else 0}",
                )
            ],
            [
                InlineKeyboardButton(
                    "📌 Follows as alerts: OFF" if use_follows else "📌 Follows as alerts: ON",
                    callback_data=f"breakfollows:{0 if use_follows else 1}",
                )
            ],
        ]
    )

    status_text = "enabled" if current_enabled else "disabled"
    custom_label = ", ".join(custom) if custom else "none"
    _ = await message.reply_text(
        f"🚨 Breaking news alerts are currently <b>{status_text}</b>.\n\n"
        f"Checks every {BREAKING_ALERT_INTERVAL_MINUTES} min for your region.\n"
        f"Sources: custom keywords ({_escape_html(custom_label)}), "
        f"{'followed topics, ' if use_follows else ''}"
        "then global defaults if empty.\n"
        f"Cap: {BREAKING_ALERT_MAX_PER_DAY}/day · quiet hours via /quiet\n"
        "Manage keywords with /breakkeywords",
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

    args = context.args or []
    raw_category = args[0] if args else ""
    raw_country = args[1] if len(args) >= 2 else None

    if await rate_limit_check(TRENDING_RATE_LIMIT_SCOPE, chat_id, TRENDING_COOLDOWN_SECONDS):
        unit = "second" if TRENDING_COOLDOWN_SECONDS == 1 else "seconds"
        _ = await message.reply_text(
            f"⏳ Trending is on cooldown. Try again in {TRENDING_COOLDOWN_SECONDS} {unit}."
        )
        return

    await rate_limit_record(TRENDING_RATE_LIMIT_SCOPE, chat_id, TRENDING_COOLDOWN_SECONDS)

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
    chat_id = _effective_chat_id(update)
    if not message:
        return

    if not is_admin_chat(chat_id):
        _ = await message.reply_text(
            "🔒 /health is admin-only. Operators: set ADMIN_CHAT_IDS and use HTTP /health."
        )
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
            logger.warning("API health check error: %s", api_health.get("error", "Unknown"))

        health_message += f"\n{db_emoji} Database: {db_health['status']}"

        if db_health["status"] == "healthy":
            health_message += f"\n   Subscribers: {db_health.get('subscriber_count', '0')}"
            health_message += f"\n   Followed topics: {db_health.get('followed_topic_count', '0')}"
            health_message += (
                f"\n   Breaking alerts tracked: {db_health.get('breaking_alert_count', '0')}"
            )
        else:
            logger.warning("Database health check error: %s", db_health.get("error", "Unknown"))

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
    """``/clear`` — confirm, then delete recent messages the bot is allowed to remove.

    In private chat this usually clears the bot's recent messages (Telegram
    limit: ~48h). It cannot wipe full Telegram history or every user message.
    """
    message = update.effective_message
    user = update.effective_user
    chat_id = _effective_chat_id(update)
    if not message or not user or chat_id is None:
        return

    keyboard = [
        [
            InlineKeyboardButton(
                "🧹 Yes, clear chat",
                callback_data=f"confirm:clear:{user.id}:{message.message_id}",
            ),
            InlineKeyboardButton("Cancel", callback_data=f"cancel:clear:{user.id}"),
        ]
    ]
    _ = await message.reply_text(
        "🧹 <b>Clear chat?</b>\n\n"
        "I'll delete recent messages I'm allowed to remove "
        "(usually <b>my messages</b> from the last ~48 hours).\n\n"
        "• Private chat: bot messages only\n"
        "• Groups: needs delete permission\n"
        "• Not a full Telegram history wipe\n\n"
        "Continue?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
