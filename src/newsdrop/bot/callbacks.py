from __future__ import annotations

from telegram import CallbackQuery, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..config import (
    CATEGORIES,
    COUNTRIES,
    DEFAULT_COUNTRY,
)
from ..database import (
    add_followed_topic,
    clear_followed_topics,
    get_user_prefs,
    remove_followed_topic,
    set_breaking_news_preference,
    set_user_prefs,
)
from ..message_utils import send_chunked_message
from ..news_fetcher import (
    APIClientError,
    format_search_results,
    search_news,
)
from ..state import (
    rate_limit_check,
    rate_limit_record,
)
from .helpers import (
    SEARCH_COOLDOWN_SECONDS,
    SEARCH_RATE_LIMIT_SCOPE,
    _clear_chat_messages,
    _country_name_from_code,
    _effective_chat_id,
    _escape_html,
    _parse_callback_data,
    _sanitize_follow_topic,
    logger,
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
        f"✅ Region set to <b>{_escape_html(name)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def _handle_category_callback(query: CallbackQuery, chat_id: int, value: str) -> None:
    if value not in CATEGORIES:
        logger.warning("Rejected invalid category in callback: %s", value)
        _ = await query.edit_message_text("⚠️ Invalid category selection.")
        return

    await set_user_prefs(chat_id, category=value)
    _ = await query.edit_message_text(
        f"✅ Category set to <b>{_escape_html(value.capitalize())}</b>",
        parse_mode=ParseMode.HTML,
    )


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

    if await rate_limit_check(SEARCH_RATE_LIMIT_SCOPE, chat_id, SEARCH_COOLDOWN_SECONDS):
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
        results = format_search_results(data_search, topic)
        _ = await status_msg.delete()
        await send_chunked_message(
            query.message,
            results,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await rate_limit_record(SEARCH_RATE_LIMIT_SCOPE, chat_id, SEARCH_COOLDOWN_SECONDS)
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
        orig_msg_id = int(parts[1])
        deleted, _ = await _clear_chat_messages(
            bot=context.bot, chat_id=chat_id, from_id=orig_msg_id, window=60
        )
        _ = await query.edit_message_text(
            f"🧹 Removed {deleted} message(s) the bot was allowed to delete "
            "(not a full chat wipe)."
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

    await query.answer()

    data = query.data or ""
    parsed = _parse_callback_data(data)
    if parsed is None:
        logger.warning("Invalid callback data format: %s", data)
        _ = await query.edit_message_text("⚠️ Invalid selection.")
        return

    action, value = parsed

    if action == "country":
        await _handle_country_callback(query, chat_id, value)
    elif action == "category":
        await _handle_category_callback(query, chat_id, value)
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
