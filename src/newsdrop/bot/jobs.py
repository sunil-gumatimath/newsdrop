from __future__ import annotations

import asyncio
import contextlib

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..config import (
    BREAKING_ALERT_KEYWORDS,
    BREAKING_ALERT_RETENTION_DAYS,
    DEFAULT_COUNTRY,
)
from ..database import (
    cleanup_old_breaking_alerts,
    get_followed_topics,
    get_user_prefs,
    load_breaking_news_subscribers,
    load_subscribers,
    mark_breaking_alert_sent,
    was_breaking_alert_sent,
)
from ..message_utils import chunk_message
from ..metrics import (
    BREAKING_ALERTS_SENT,
    DAILY_MESSAGES_SENT,
    increment,
)
from ..news_fetcher import (
    APIClientError,
    fetch_breaking_news,
    fetch_top_headlines,
)
from .helpers import (
    _build_digest_payload,
    _get_article_key,
    _safe_url,
    _send_article,
    logger,
)


async def send_breaking_news_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = await load_breaking_news_subscribers()
    if not subscribers:
        logger.info("No users opted into breaking news alerts.")
        return

    if not BREAKING_ALERT_KEYWORDS:
        logger.info("Breaking news alerts are disabled because no keywords are configured.")
        return

    country_to_chats: dict[str, list[int]] = {}
    for chat_id in subscribers:
        prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
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
        await cleanup_old_breaking_alerts(BREAKING_ALERT_RETENTION_DAYS)
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

            if await was_breaking_alert_sent(chat_id, article_key):
                continue

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🚨 <b>Breaking News Alert</b>",
                    parse_mode=ParseMode.HTML,
                )
                await _send_article(context.bot, chat_id, article, 1)
                if await mark_breaking_alert_sent(chat_id, article_key, url, title):
                    sent_count += 1
                    await increment(BREAKING_ALERTS_SENT)
            except Exception as exc:
                logger.exception(
                    "Failed to send breaking alert to %s: %s",
                    chat_id,
                    exc,
                )

    logger.info("Sent %s breaking news alert(s).", sent_count)


async def _send_combo(
    context: ContextTypes.DEFAULT_TYPE,
    grouped: dict[tuple[str, str], list[int]],
    semaphore: asyncio.Semaphore,
) -> None:
    """Fetch once per (country, category) combo and send to all subscribers in that group."""
    for (country, category), chat_ids in grouped.items():
        async with semaphore:
            try:
                data = await fetch_top_headlines(country, category)
            except APIClientError as exc:
                logger.error(
                    "News API error fetching %s/%s: %s", country, category, exc
                )
                for chat_id in chat_ids:
                    with contextlib.suppress(Exception):
                        _ = await context.bot.send_message(
                            chat_id=chat_id,
                            text="⚠️ Could not fetch today's news. Please try again later.",
                        )
                continue
            except Exception as exc:
                logger.exception(
                    "Unexpected error fetching %s/%s: %s", country, category, exc
                )
                continue

            # Build a digest per user (followed topics differ per user).
            # But reuse the fetched `data` across the whole group.
            for chat_id in chat_ids:
                try:
                    followed = await get_followed_topics(chat_id)
                    digest, empty_message = _build_digest_payload(
                        data, category, country, followed
                    )

                    if empty_message:
                        _ = await context.bot.send_message(
                            chat_id=chat_id,
                            text=empty_message,
                            parse_mode=ParseMode.HTML,
                        )
                        continue

                    assert digest is not None
                    if len(digest) <= 4096:
                        _ = await context.bot.send_message(
                            chat_id=chat_id,
                            text=digest,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                        await increment(DAILY_MESSAGES_SENT)
                    else:
                        chunks = chunk_message(digest)
                        for chunk in chunks:
                            _ = await context.bot.send_message(
                                chat_id=chat_id,
                                text=chunk,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True,
                            )
                        await increment(DAILY_MESSAGES_SENT)

                except Exception as exc:
                    logger.exception("Failed to send news to %s: %s", chat_id, exc)


async def send_daily_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = await load_subscribers()
    if not subscribers:
        logger.info("No subscribers to send daily news to.")
        return

    logger.info("Sending daily news to %s subscribers...", len(subscribers))

    # Group subscribers by (country, category) so we only fetch once per combo.
    # This reduces API calls from N (one per user) to ~M (unique combos).
    grouped: dict[tuple[str, str], list[int]] = {}
    for chat_id in subscribers:
        prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
        country = prefs.get("country", DEFAULT_COUNTRY)
        category = prefs.get("category", "general")
        grouped.setdefault((country, category), []).append(chat_id)

    logger.info(
        "Grouped %s subscribers into %s unique (country, category) combos.",
        len(subscribers),
        len(grouped),
    )

    # Fetch once per combo and reuse the digest for all users in that group.
    # Limit concurrency to avoid hammering the API.
    semaphore = asyncio.Semaphore(3)
    await _send_combo(context, grouped, semaphore)
