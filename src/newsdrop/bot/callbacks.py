from __future__ import annotations

import contextlib

from telegram import CallbackQuery, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..config import (
    CATEGORIES,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DEFAULT_DAILY_HOUR,
    DEFAULT_TIMEZONE,
)
from ..database import (
    add_followed_topic,
    add_subscriber,
    clear_followed_topics,
    get_followed_topics,
    get_user_prefs,
    is_subscriber,
    remove_followed_topic,
    set_breaking_news_preference,
    set_user_prefs,
)
from ..metrics import COMMAND_NEWS, COMMAND_TOTAL, NEWS_API_ERRORS, increment
from ..news_fetcher import (
    APIClientError,
    fetch_top_headlines,
    search_news,
)
from ..state import (
    rate_limit_try_acquire,
)
from .helpers import (
    NEWS_COOLDOWN_SECONDS as _NEWS_COOLDOWN_SECONDS,
)
from .helpers import (
    NEWS_RATE_LIMIT_SCOPE,
    SEARCH_COOLDOWN_SECONDS,
    SEARCH_RATE_LIMIT_SCOPE,
    _build_digest_payload,
    _clear_chat_messages,
    _country_name_from_code,
    _effective_chat_id,
    _escape_html,
    _parse_callback_data,
    _sanitize_follow_topic,
    build_search_payload,
    category_keyboard,
    country_keyboard,
    logger,
    onboarding_finish_keyboard,
)


async def _handle_country_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    valid_codes = set(COUNTRIES.values())
    if value not in valid_codes:
        logger.warning("Rejected invalid country code in callback: %s", value)
        _ = await query.edit_message_text("⚠️ Invalid region selection.")
        return

    name = _country_name_from_code(value)
    await set_user_prefs(chat_id, country=value)
    _ = await query.edit_message_text(
        f"✅ Region set to <b>{_escape_html(name)}</b>\n\n"
        "Use /setcategory to pick a topic, or /news for a briefing.",
        parse_mode=ParseMode.HTML,
    )


async def _handle_category_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    if value not in CATEGORIES:
        logger.warning("Rejected invalid category in callback: %s", value)
        _ = await query.edit_message_text("⚠️ Invalid category selection.")
        return

    await set_user_prefs(chat_id, category=value)
    _ = await query.edit_message_text(
        f"✅ Category set to <b>{_escape_html(value.capitalize())}</b>\n\n"
        "Tap /news for a briefing, or /subscribe for daily delivery.",
        parse_mode=ParseMode.HTML,
    )


async def _handle_obcountry_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    valid_codes = set(COUNTRIES.values())
    if value not in valid_codes:
        _ = await query.edit_message_text("⚠️ Invalid region selection.")
        return

    name = _country_name_from_code(value)
    await set_user_prefs(chat_id, country=value)
    _ = await query.edit_message_text(
        f"✅ Region: <b>{_escape_html(name)}</b>\n\n<b>Step 2 of 3 — pick a category</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=category_keyboard(
            onboarding=True,
            user_id=query.from_user.id if query.from_user else None,
        ),
    )


async def _handle_obcategory_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    if value not in CATEGORIES:
        _ = await query.edit_message_text("⚠️ Invalid category selection.")
        return

    await set_user_prefs(chat_id, category=value)
    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    region = _country_name_from_code(prefs.get("country", DEFAULT_COUNTRY))
    _ = await query.edit_message_text(
        f"✅ Feed ready\n"
        f"🌍 {_escape_html(region)}  ·  📂 {_escape_html(value.capitalize())}\n\n"
        "<b>Step 3 of 3 — daily delivery?</b>\n"
        "Subscribe for an automatic briefing at your local hour "
        f"(default {DEFAULT_DAILY_HOUR}:00 {DEFAULT_TIMEZONE}).\n"
        "Change anytime with /settime and /settimezone.",
        parse_mode=ParseMode.HTML,
        reply_markup=onboarding_finish_keyboard(),
    )


async def _handle_obsub_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    if value == "1":
        if not await is_subscriber(chat_id):
            await add_subscriber(chat_id)
        prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
        hour = prefs.get("daily_hour", str(DEFAULT_DAILY_HOUR))
        tz = prefs.get("timezone", DEFAULT_TIMEZONE)
        _ = await query.edit_message_text(
            f"🎉 <b>You're set!</b>\n\n"
            f"✅ Daily briefing around <b>{_escape_html(str(hour))}:00</b> "
            f"({_escape_html(str(tz))})\n"
            f"🌍 {_escape_html(prefs.get('country', DEFAULT_COUNTRY).upper())}  ·  "
            f"📂 {_escape_html(prefs.get('category', 'general').capitalize())}\n\n"
            "Try /news now · /follow AI · /breaking for alerts · /help",
            parse_mode=ParseMode.HTML,
        )
        return

    _ = await query.edit_message_text(
        "👍 Setup complete without a daily subscription.\n\n"
        "Use /news anytime · /subscribe later · /help for more.",
        parse_mode=ParseMode.HTML,
    )


# Actions whose buttons mutate chat state or trigger API spend. Their callback
# payloads carry the originating user's id as a trailing ":<user_id>" segment.
_OWNED_CALLBACK_ACTIONS = frozenset(
    {
        "country",
        "category",
        "obcountry",
        "obcategory",
        "obsub",
        "menu",
        "breaking",
        "breakfollows",
        "dailyhour",
        "tz",
        "follow",
        "unfollow",
    }
)


def _extract_ownership_user_id(update: Update, action: str) -> int | None:
    """Return the originating user id embedded in *action* callbacks, if any.

    Only ``country:<code>:<user_id>``-style payloads (an extra trailing numeric
    segment on an owned action) yield a value; legacy two-segment payloads and
    confirm/cancel (which verify ownership themselves) return None.
    """
    if action in ("confirm", "cancel"):
        # Verified inside _handle_confirm_or_cancel.
        return None
    if action not in _OWNED_CALLBACK_ACTIONS:
        return None

    query = update.callback_query
    if query is None or not query.data:
        return None
    parts = query.data.split(":")
    # Owned actions are "<action>:<value>[:<user_id>]". A trailing pure-integer
    # third segment marks the owner.
    if len(parts) >= 3 and parts[-1].isdigit():
        try:
            return int(parts[-1])
        except ValueError:  # pragma: no cover - isdigit guarantees int()
            return None
    return None


async def _handle_obnews_callback(query: CallbackQuery, chat_id: int) -> None:
    """Finish onboarding by sending a live briefing in-place."""
    if not await rate_limit_try_acquire(
        NEWS_RATE_LIMIT_SCOPE, chat_id, _NEWS_COOLDOWN_SECONDS
    ):
        await increment(COMMAND_NEWS)
        _ = await query.answer(
            f"⏳ Please wait {_NEWS_COOLDOWN_SECONDS}s before requesting news again.",
            show_alert=True,
        )
        return
    await increment(COMMAND_TOTAL)
    await increment(COMMAND_NEWS)
    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    country = prefs.get("country", DEFAULT_COUNTRY)
    category = prefs.get("category", "general")
    followed = await get_followed_topics(chat_id)

    _ = await query.edit_message_text("📰 Fetching your first briefing...")
    try:
        data = await fetch_top_headlines(country, category)
        result = _build_digest_payload(data, category, country, followed)
        if result.empty_message:
            _ = await query.edit_message_text(
                result.empty_message,
                parse_mode=ParseMode.HTML,
                reply_markup=result.reply_markup,
            )
            return
        if result.digest is None:
            return
        # Telegram edit_message_text max is 4096; fall back to truncated if needed.
        text = result.digest
        if len(text) > 4000:
            text = text[:3990] + "…"
        _ = await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=result.reply_markup,
        )
    except APIClientError:
        await increment(NEWS_API_ERRORS)
        _ = await query.edit_message_text(
            "🔧 Could not fetch news right now. Try /news in a moment."
        )
    except Exception:
        logger.exception("Onboarding news fetch failed")
        _ = await query.edit_message_text("🔧 Something went wrong. Try /news in a moment.")


async def _handle_menu_callback(query: CallbackQuery, value: str) -> None:
    if value == "country":
        _ = await query.edit_message_text(
            "🌍 Select your news region:",
            reply_markup=country_keyboard(
                onboarding=False,
                user_id=query.from_user.id if query.from_user else None,
            ),
        )
    elif value == "category":
        _ = await query.edit_message_text(
            "📂 Select your news category:",
            reply_markup=category_keyboard(
                onboarding=False,
                user_id=query.from_user.id if query.from_user else None,
            ),
        )
    elif value == "search_hint":
        _ = await query.edit_message_text(
            "🔍 Search for a topic:\n\n<code>/search bitcoin</code>\n<code>/search climate</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        _ = await query.edit_message_text("⚠️ Unknown menu action.")


async def _handle_breaking_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    enabled = value == "1"
    await set_breaking_news_preference(chat_id, enabled)
    status_text = "enabled" if enabled else "disabled"
    _ = await query.edit_message_text(
        f"✅ Breaking news alerts <b>{status_text}</b>",
        parse_mode=ParseMode.HTML,
    )


async def _handle_breakfollows_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    enabled = value == "1"
    await set_user_prefs(chat_id, breaking_use_follows=enabled)
    status = "ON" if enabled else "OFF"
    _ = await query.edit_message_text(
        f"✅ Using followed topics as breaking alerts: <b>{status}</b>",
        parse_mode=ParseMode.HTML,
    )


async def _handle_dailyhour_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    try:
        hour = int(value)
    except ValueError:
        _ = await query.edit_message_text("⚠️ Invalid hour.")
        return
    if not (0 <= hour <= 23):
        _ = await query.edit_message_text("⚠️ Invalid hour.")
        return
    await set_user_prefs(chat_id, daily_hour=hour)
    _ = await query.edit_message_text(
        f"✅ Daily digest hour set to <b>{hour:02d}:00</b> (your timezone).",
        parse_mode=ParseMode.HTML,
    )


async def _handle_tz_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    tz_name = value.strip()
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz_name)
    except Exception:
        _ = await query.edit_message_text("⚠️ Invalid timezone.")
        return
    await set_user_prefs(chat_id, timezone=tz_name)
    _ = await query.edit_message_text(
        f"✅ Timezone set to <b>{_escape_html(tz_name)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def _handle_search_callback(
    query: CallbackQuery,
    chat_id: int,
    value: str,
) -> None:
    topic = _sanitize_follow_topic(value)
    if not topic:
        _ = await query.edit_message_text("⚠️ Invalid search topic.")
        return

    if not query.message:
        _ = await query.edit_message_text("⚠️ Search message is unavailable.")
        return

    if not await rate_limit_try_acquire(SEARCH_RATE_LIMIT_SCOPE, chat_id, SEARCH_COOLDOWN_SECONDS):
        _ = await query.answer(
            f"⏳ Please wait {SEARCH_COOLDOWN_SECONDS}s before searching again.",
            show_alert=True,
        )
        return

    prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
    country = prefs.get("country", DEFAULT_COUNTRY)
    status_msg = await query.message.reply_text(  # type: ignore[attr-defined]
        f'🔍 Searching for "{topic}"...'
    )

    try:
        data_search = await search_news(topic, country)
        result = build_search_payload(data_search, topic)
        if result.empty_message:
            _ = await status_msg.edit_text(
                result.empty_message,
                parse_mode=ParseMode.HTML,
                reply_markup=result.reply_markup,
            )
        elif result.digest is not None:
            text = result.digest if len(result.digest) <= 4096 else result.digest[:3990] + "…"
            _ = await status_msg.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=result.reply_markup,
            )
        # rate limit was already acquired atomically at the top of this handler
    except APIClientError:
        logger.exception("Callback search failed")
        _ = await status_msg.edit_text("🔧 Could not fetch news. Please try again later.")
    except Exception as exc:
        logger.exception("Unexpected error searching news: %s", exc)
        _ = await status_msg.edit_text("🔧 An unexpected error occurred. Please try again later.")


async def _handle_follow_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    topic = _sanitize_follow_topic(value)
    created, result = await add_followed_topic(chat_id, topic)
    if query.message:
        if created:
            _ = await query.message.reply_text(  # type: ignore[attr-defined]
                f"✅ Now following <b>{_escape_html(result)}</b>.",
                parse_mode=ParseMode.HTML,
            )
        else:
            _ = await query.message.reply_text(f"⚠️ {result}")  # type: ignore[attr-defined]


async def _handle_unfollow_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    topic = _sanitize_follow_topic(value)
    removed = await remove_followed_topic(chat_id, topic)
    if query.message:
        if removed:
            _ = await query.message.reply_text(  # type: ignore[attr-defined]
                f"✅ Unfollowed <b>{_escape_html(topic)}</b>.",
                parse_mode=ParseMode.HTML,
            )
        else:
            _ = await query.message.reply_text(  # type: ignore[attr-defined]
                "⚠️ You are not following that topic."
            )


async def _handle_confirm_or_cancel(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    action: str,
    value: str,
    data: str,
) -> None:
    """Handle confirm/cancel callback actions (clear chat, unfollow all)."""
    kind_and_rest = value.split(":", 1)
    if len(kind_and_rest) != 2:
        logger.warning("Malformed confirm/cancel callback: %s", data)
        _ = await query.edit_message_text("⚠️ Invalid selection.")
        return

    kind, rest = kind_and_rest

    user_id_str = rest.split(":", 1)[0]
    try:
        expected_user_id = int(user_id_str)
    except ValueError:
        logger.warning("Malformed user_id in callback: %s", user_id_str)
        _ = await query.edit_message_text("⚠️ Invalid selection.")
        return

    if query.from_user.id != expected_user_id:
        await query.answer("This confirmation is not for you.", show_alert=True)
        return

    if action == "cancel":
        _ = await query.edit_message_text("❌ Cancelled.")
        return

    if kind == "clear":
        parts = rest.split(":")
        if len(parts) != 2:
            logger.warning("Malformed clear callback: %s", data)
            _ = await query.edit_message_text("⚠️ Invalid selection.")
            return
        try:
            orig_msg_id = int(parts[1])
        except ValueError:
            _ = await query.edit_message_text("⚠️ Invalid selection.")
            return

        # Start from the confirmation message (newest), not only /clear.
        confirm_id = query.message.message_id if query.message else orig_msg_id
        start_id = max(confirm_id, orig_msg_id)

        with contextlib.suppress(Exception):
            _ = await query.edit_message_text("🧹 Clearing chat…")

        deleted, _ = await _clear_chat_messages(
            bot=context.bot,
            chat_id=chat_id,
            from_id=start_id,
            window=150,
        )

        # Confirmation may already be gone; always send a fresh status.
        status = (
            f"🧹 <b>Chat cleared</b>\n"
            f"Removed <b>{deleted}</b> message(s) I could delete.\n\n"
            "Tip: your typed messages may remain (Telegram only lets me "
            "delete my own in private chats). Use /news to start fresh."
            if deleted
            else (
                "🧹 Nothing new to delete (messages may be too old, already "
                "gone, or not mine).\n\n"
                "In private chat I can only remove <b>my</b> recent messages."
            )
        )
        try:
            if query.message:
                _ = await query.edit_message_text(status, parse_mode=ParseMode.HTML)
            else:
                _ = await context.bot.send_message(
                    chat_id=chat_id, text=status, parse_mode=ParseMode.HTML
                )
        except Exception:
            with contextlib.suppress(Exception):
                _ = await context.bot.send_message(
                    chat_id=chat_id, text=status, parse_mode=ParseMode.HTML
                )
        return

    if kind == "unfollowall":
        removed_count = await clear_followed_topics(chat_id)
        if removed_count > 0:
            _ = await query.edit_message_text(f"✅ Removed all followed topics ({removed_count}).")
        else:
            _ = await query.edit_message_text("You were not following any topics.")
        return

    logger.warning("Unknown confirm kind: %s", kind)
    _ = await query.edit_message_text("⚠️ Unsupported action.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = _effective_chat_id(update)
    if not query:
        return

    if chat_id is None:
        await query.answer()
        return

    data = query.data or ""

    # Ownership check: state-changing callbacks carry the originating user's
    # id as a trailing ":<user_id>" segment. Ignore taps from anyone else
    # (e.g. group members tapping old buttons). Legacy payloads without the
    # suffix are still accepted for backward compatibility.
    parsed = _parse_callback_data(data)
    if parsed is None:
        logger.warning("Invalid callback data format: %s", data)
        _ = await query.edit_message_text("⚠️ Invalid selection.")
        return

    action, value = parsed
    expected_user_id = _extract_ownership_user_id(update, action)
    if (
        expected_user_id is not None
        and query.from_user is not None
        and query.from_user.id != expected_user_id
    ):
        await query.answer("Not your session.", show_alert=True)
        return

    # Answer the query once, here, for all branches that do NOT answer it
    # themselves (e.g. the search rate-limit alert answers its own query).
    if action != "search":
        await query.answer()

    if action == "country":
        await _handle_country_callback(query, chat_id, value)
    elif action == "category":
        await _handle_category_callback(query, chat_id, value)
    elif action == "obcountry":
        await _handle_obcountry_callback(query, chat_id, value)
    elif action == "obcategory":
        await _handle_obcategory_callback(query, chat_id, value)
    elif action == "obsub":
        await _handle_obsub_callback(query, chat_id, value)
    elif action == "obnews":
        await _handle_obnews_callback(query, chat_id)
    elif action == "menu":
        await _handle_menu_callback(query, value)
    elif action == "breaking":
        await _handle_breaking_callback(query, chat_id, value)
    elif action == "breakfollows":
        await _handle_breakfollows_callback(query, chat_id, value)
    elif action == "dailyhour":
        await _handle_dailyhour_callback(query, chat_id, value)
    elif action == "tz":
        await _handle_tz_callback(query, chat_id, value)
    elif action == "search":
        await _handle_search_callback(query, chat_id, value)
    elif action == "follow":
        await _handle_follow_callback(query, chat_id, value)
    elif action == "unfollow":
        await _handle_unfollow_callback(query, chat_id, value)
    elif action in ("confirm", "cancel"):
        await _handle_confirm_or_cancel(query, context, chat_id, action, value, data)
    else:
        logger.warning("Unhandled callback action: %s", action)
        _ = await query.edit_message_text("⚠️ Unsupported action.")
