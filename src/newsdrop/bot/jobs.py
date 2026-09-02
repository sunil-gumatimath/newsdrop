from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import ContextTypes

from ..config import (
    BREAKING_ALERT_KEYWORDS,
    BREAKING_ALERT_MAX_PER_DAY,
    BREAKING_ALERT_RETENTION_DAYS,
    BREAKING_USE_FOLLOWED_TOPICS,
    DEFAULT_COUNTRY,
    DEFAULT_DAILY_HOUR,
    DEFAULT_TIMEZONE,
)
from ..database import (
    claim_breaking_alert_slot,
    cleanup_old_breaking_alerts,
    count_breaking_alerts_today,
    get_followed_topics,
    get_user_prefs,
    load_all_user_ids,
    load_breaking_news_subscribers,
    parse_breaking_keywords,
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
    format_breaking_alert,
    logger,
)


def _safe_zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _parse_digest_days_str(raw: str) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            continue
        if 0 <= n <= 6 and n not in seen:
            seen.add(n)
            out.append(n)
    out.sort()
    return out


def is_digest_due(prefs: dict[str, str], now: datetime | None = None) -> bool:
    """Return True if the user's local hour/day matches their digest schedule."""
    now = now or datetime.now(UTC)
    tz = _safe_zoneinfo(prefs.get("timezone") or DEFAULT_TIMEZONE)
    local = now.astimezone(tz)
    freq = str(prefs.get("digest_frequency") or "daily").strip().lower()
    if freq not in {"daily", "twice", "weekdays", "custom"}:
        freq = "daily"
    try:
        preferred_hour = int(prefs.get("daily_hour") or DEFAULT_DAILY_HOUR)
    except ValueError:
        preferred_hour = DEFAULT_DAILY_HOUR
    preferred_hour = max(0, min(23, preferred_hour))

    if freq == "twice":
        # Spec: twice = 8am + 8pm local.
        return local.hour in (8, 20)

    if freq == "weekdays":
        # Mon-Fri only (weekday 0-4).
        if local.weekday() >= 5:
            return False
        return local.hour == preferred_hour

    if freq == "custom":
        days = _parse_digest_days_str(prefs.get("digest_days", "") or "")
        if not days:
            # No days configured → fall back to daily so user still gets digests.
            return local.hour == preferred_hour
        if local.weekday() not in days:
            return False
        return local.hour == preferred_hour

    return local.hour == preferred_hour


def is_in_quiet_hours(prefs: dict[str, str], now: datetime | None = None) -> bool:
    """Return True when breaking alerts should be suppressed for this user."""
    start_raw = prefs.get("quiet_start_hour", "")
    end_raw = prefs.get("quiet_end_hour", "")
    if start_raw == "" or end_raw == "":
        return False
    try:
        start = int(start_raw)
        end = int(end_raw)
    except ValueError:
        return False
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return False
    if start == end:
        return False

    now = now or datetime.now(UTC)
    tz = _safe_zoneinfo(prefs.get("timezone") or DEFAULT_TIMEZONE)
    local_hour = now.astimezone(tz).hour

    if start < end:
        return start <= local_hour < end
    # Wraps midnight, e.g. 22 → 7
    return local_hour >= start or local_hour < end


def resolve_alert_keywords(
    prefs: dict[str, str],
    followed_topics: list[str],
) -> list[str]:
    """Build the keyword list used to match breaking articles for one user."""
    custom = parse_breaking_keywords(prefs.get("breaking_keywords", ""))
    use_follows = BREAKING_USE_FOLLOWED_TOPICS and prefs.get("breaking_use_follows", "1") != "0"

    keywords: list[str] = []
    seen: set[str] = set()

    def _add(items: list[str]) -> None:
        for item in items:
            cleaned = " ".join(item.strip().split())
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            keywords.append(cleaned)

    _add(custom)
    if use_follows:
        _add(followed_topics)
    # Global defaults only when the user has no personal signal.
    if not keywords:
        _add(BREAKING_ALERT_KEYWORDS)
    return keywords


def _keyword_pattern(kw: str) -> re.Pattern[str]:
    """Whole-word pattern for breaking keywords, supports multi-word phrases."""
    return re.compile(rf"(?<![a-z0-9]){re.escape(kw.strip().lower())}(?![a-z0-9])", re.IGNORECASE)


def matching_alert_keywords(article: dict, keywords: list[str]) -> list[str]:
    """Return keywords that trigger an alert for this article (quieter rules).

    Prefer a **title** hit. Body-only matches need ≥2 keywords so one weak
    word in a description does not fire a noisy alert.
    """
    if not keywords:
        return []
    title = str(article.get("title", "")).lower()
    description = str(article.get("description", "")).lower()
    combined = f"{title} {description}"
    matched = [kw for kw in keywords if _keyword_pattern(kw).search(combined)]
    if not matched:
        return []
    title_hits = [kw for kw in matched if _keyword_pattern(kw).search(title)]
    if title_hits:
        return title_hits
    if len(matched) >= 2:
        return matched
    return []


def article_matches_keywords(article: dict, keywords: list[str]) -> bool:
    return bool(matching_alert_keywords(article, keywords))


async def send_breaking_news_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = await load_breaking_news_subscribers()
    if not subscribers:
        logger.info("No users opted into breaking news alerts.")
        return

    # Prefs + keywords per chat; also collect countries and union keywords for fetch.
    chat_prefs: dict[int, dict[str, str]] = {}
    chat_keywords: dict[int, list[str]] = {}
    country_to_chats: dict[str, list[int]] = {}
    country_keyword_union: dict[str, set[str]] = {}

    for chat_id in subscribers:
        prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
        chat_prefs[chat_id] = prefs
        if is_in_quiet_hours(prefs):
            continue

        followed = await get_followed_topics(chat_id)
        keywords = resolve_alert_keywords(prefs, followed)
        if not keywords:
            continue

        chat_keywords[chat_id] = keywords
        country = prefs.get("country", DEFAULT_COUNTRY)
        country_to_chats.setdefault(country, []).append(chat_id)
        country_keyword_union.setdefault(country, set()).update(k.lower() for k in keywords)

    countries = list(country_to_chats.keys())
    if not countries:
        logger.info("No breaking-alert recipients due after quiet hours / keyword filter.")
        return

    # Union of all keywords for the API/RSS scan, then filter per user.
    all_keywords = sorted({kw for kws in country_keyword_union.values() for kw in kws})
    if not all_keywords and not BREAKING_ALERT_KEYWORDS:
        logger.info("Breaking news alerts are disabled because no keywords are configured.")
        return
    fetch_keywords = all_keywords or [k.lower() for k in BREAKING_ALERT_KEYWORDS]

    logger.info(
        "Checking breaking news for %s opted-in user(s) across %s region(s).",
        len(chat_keywords),
        len(countries),
    )

    try:
        await cleanup_old_breaking_alerts(BREAKING_ALERT_RETENTION_DAYS)
        articles = await fetch_breaking_news(countries, fetch_keywords)
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
    per_user_sent: dict[int, int] = {}

    for article in articles[:30]:
        country = str(article.get("country", ""))
        chat_ids = country_to_chats.get(country, [])
        article_key = _get_article_key(article)

        if not article_key:
            continue

        for chat_id in chat_ids:
            keywords = chat_keywords.get(chat_id, [])
            matched_kws = matching_alert_keywords(article, keywords)
            if not matched_kws:
                continue

            title = str(article.get("title", ""))
            url = _safe_url(article.get("url", ""))

            # Atomic cap + dedupe gate: claim the slot BEFORE sending. The
            # claim counts today's rows inside the same transaction as the
            # dedupe insert, so neither the daily cap nor duplicate sends can
            # be raced by overlapping job runs / concurrent workers.
            if not await claim_breaking_alert_slot(
                chat_id,
                article_key,
                url,
                title,
                max_per_day=BREAKING_ALERT_MAX_PER_DAY,
            ):
                continue
            next_count = await count_breaking_alerts_today(chat_id)

            try:
                text, markup = format_breaking_alert(
                    article,
                    matched_kws,
                    used_today=next_count,
                    max_per_day=BREAKING_ALERT_MAX_PER_DAY,
                )
                send_kwargs: dict = {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": ParseMode.HTML,
                    "disable_web_page_preview": True,
                }
                if isinstance(markup, InlineKeyboardMarkup):
                    send_kwargs["reply_markup"] = markup
                try:
                    await context.bot.send_message(**send_kwargs)
                except RetryAfter as exc:
                    # Telegram flood control: back off for the requested
                    # duration, then retry exactly once. Retrying cannot
                    # cause duplicates because the mark-first gate above
                    # already claimed this (chat_id, article_key) slot.
                    logger.warning(
                        "Rate limited sending breaking alert to %s, retrying once after %s s: %s",
                        chat_id,
                        exc.retry_after,
                        exc,
                    )
                    # PTB types retry_after as ``int | timedelta``.
                    if isinstance(exc.retry_after, timedelta):
                        delay = exc.retry_after.total_seconds()
                    else:
                        delay = float(exc.retry_after)
                    await asyncio.sleep(max(delay, 0.0))
                    await context.bot.send_message(**send_kwargs)
                sent_count += 1
                per_user_sent[chat_id] = next_count
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
    """Fetch once per (country, category) combo and send to all subscribers in that group.

    Parallelized with semaphore-gated concurrency: each combo runs as a
    separate task and acquires the semaphore only for its fetch+send work.
    The previous sequential ``for combo: async with semaphore`` never
    overlapped fetches.
    """

    async def _send_one(country: str, category: str, chat_ids: list[int]) -> None:
        async with semaphore:
            try:
                data = await fetch_top_headlines(country, category)
            except APIClientError as exc:
                logger.error("News API error fetching %s/%s: %s", country, category, exc)
                for chat_id in chat_ids:
                    with contextlib.suppress(Exception):
                        _ = await context.bot.send_message(
                            chat_id=chat_id,
                            text="⚠️ Could not fetch today's news. Please try again later.",
                        )
                return
            except Exception as exc:
                logger.exception("Unexpected error fetching %s/%s: %s", country, category, exc)
                return

            # Build a digest per user (followed topics differ per user).
            # But reuse the fetched `data` across the whole group.
            for chat_id in chat_ids:
                try:
                    followed = await get_followed_topics(chat_id)
                    result = _build_digest_payload(data, category, country, followed)

                    if result.empty_message:
                        _ = await context.bot.send_message(
                            chat_id=chat_id,
                            text=result.empty_message,
                            parse_mode=ParseMode.HTML,
                            reply_markup=result.reply_markup,
                        )
                        continue

                    if result.digest is None:
                        continue

                    if len(result.digest) <= 4096:
                        _ = await context.bot.send_message(
                            chat_id=chat_id,
                            text=result.digest,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                            reply_markup=result.reply_markup,
                        )
                        await increment(DAILY_MESSAGES_SENT)
                    else:
                        chunks = chunk_message(result.digest)
                        for chunk in chunks:
                            _ = await context.bot.send_message(
                                chat_id=chat_id,
                                text=chunk,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True,
                            )
                        if result.reply_markup is not None:
                            with contextlib.suppress(Exception):
                                _ = await context.bot.send_message(
                                    chat_id=chat_id,
                                    text="📖 Open full articles:",
                                    reply_markup=result.reply_markup,
                                )
                        await increment(DAILY_MESSAGES_SENT)

                except Exception as exc:
                    logger.exception("Failed to send news to %s: %s", chat_id, exc)

    tasks = [
        asyncio.create_task(_send_one(country, category, chat_ids))
        for (country, category), chat_ids in grouped.items()
    ]
    if tasks:
        await asyncio.gather(*tasks)


async def send_daily_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send digests to subscribers whose local preferred hour is now."""
    user_ids = await load_all_user_ids()
    if not user_ids:
        logger.info("No users to send daily news to.")
        return

    due: list[int] = []
    for chat_id in user_ids:
        prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
        if is_digest_due(prefs):
            due.append(chat_id)

    if not due:
        logger.info(
            "No users due for daily news this hour (checked %s).",
            len(user_ids),
        )
        return

    logger.info("Sending daily news to %s due subscriber(s)...", len(due))

    # Group subscribers by (country, category) so we only fetch once per combo.
    grouped: dict[tuple[str, str], list[int]] = {}
    for chat_id in due:
        prefs = await get_user_prefs(chat_id, DEFAULT_COUNTRY)
        country = prefs.get("country", DEFAULT_COUNTRY)
        category = prefs.get("category", "general")
        grouped.setdefault((country, category), []).append(chat_id)

    logger.info(
        "Grouped %s users into %s unique (country, category) combos.",
        len(due),
        len(grouped),
    )

    semaphore = asyncio.Semaphore(3)
    await _send_combo(context, grouped, semaphore)
