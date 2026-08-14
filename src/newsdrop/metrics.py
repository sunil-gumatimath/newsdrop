"""Named metric counters for operational visibility.

Counters are stored in the shared state backend so they work across
workers when Redis is configured. Also exposes windowed counters
(1-hour and 24-hour sliding windows) for rate-based alerting.
"""

from __future__ import annotations

from .state import WindowedCounter
from .state import metric_get as _metric_get
from .state import metric_increment as _metric_increment

COMMAND_NEWS = "command:news"
COMMAND_SEARCH = "command:search"
COMMAND_FOLLOW = "command:follow"
COMMAND_UNFOLLOW = "command:unfollow"
COMMAND_SUBSCRIBE = "command:subscribe"
COMMAND_UNSUBSCRIBE = "command:unsubscribe"
COMMAND_BREAKING_TOGGLE = "command:breaking_toggle"
COMMAND_TRENDING = "command:trending"
COMMAND_HEALTH = "command:health"
COMMAND_TOTAL = "command:total"

DAILY_MESSAGES_SENT = "daily:messages_sent"
BREAKING_ALERTS_SENT = "breaking:alerts_sent"

NEWS_API_ERRORS = "errors:news_api"
UNEXPECTED_ERRORS = "errors:unexpected"

# Windowed rate counters are keyed by a base name (used by callers of
# ``increment`` and ``get_rate``), mapped to the canonical metric constant so
# the sliding-window records actually line up with the named counter.
_WINDOWED_NAME_TO_METRIC = {
    "news_api_errors": NEWS_API_ERRORS,
    "unexpected_errors": UNEXPECTED_ERRORS,
    "daily_messages_sent": DAILY_MESSAGES_SENT,
    "breaking_alerts_sent": BREAKING_ALERTS_SENT,
    "command_total": COMMAND_TOTAL,
}

# Names eligible for sliding-window rate tracking (derived from the real
# constants above so the ``rate:{name}`` key always matches ``increment``).
_windowed_names = {f"rate:{name}" for name in _WINDOWED_NAME_TO_METRIC.values()}


def _windowed_rate_key(name: str) -> str | None:
    """Return the rate key for ``name``, or ``None`` if not windowed.

    ``name`` may be either the canonical constant (e.g. ``errors:news_api``)
    or the short base name used by callers (e.g. ``news_api_errors``).
    """
    if f"rate:{name}" in _windowed_names:
        return f"rate:{name}"
    metric = _WINDOWED_NAME_TO_METRIC.get(name)
    if metric is not None:
        return f"rate:{metric}"
    return None


_windowed_counters: dict[str, WindowedCounter] = {}


def _get_windowed(rate_key: str) -> WindowedCounter:
    if rate_key not in _windowed_counters:
        _windowed_counters[rate_key] = WindowedCounter(rate_key, windows=(3600, 86400))
    return _windowed_counters[rate_key]


async def increment(name: str, value: int = 1) -> None:
    """Increment a counter by ``value``."""
    await _metric_increment(name, value)
    # Track rate windows for error and high-volume metrics.
    rate_key = _windowed_rate_key(name)
    if rate_key is not None:
        await _get_windowed(rate_key).increment(value)


async def get(name: str) -> int:
    """Return the current value of a counter."""
    return await _metric_get(name)


async def get_rate(name: str, window_seconds: int = 3600) -> int:
    """Return the number of increments in the given sliding window.

    window_seconds must be one of 3600 (1h) or 86400 (24h).
    """
    rate_key = _windowed_rate_key(name)
    if rate_key is None:
        return 0
    return await _get_windowed(rate_key).count_window(window_seconds)


async def all_metrics() -> dict[str, int]:
    """Return all known counters."""
    names = [
        COMMAND_TOTAL,
        COMMAND_NEWS,
        COMMAND_SEARCH,
        COMMAND_FOLLOW,
        COMMAND_UNFOLLOW,
        COMMAND_SUBSCRIBE,
        COMMAND_UNSUBSCRIBE,
        COMMAND_BREAKING_TOGGLE,
        COMMAND_TRENDING,
        COMMAND_HEALTH,
        DAILY_MESSAGES_SENT,
        BREAKING_ALERTS_SENT,
        NEWS_API_ERRORS,
        UNEXPECTED_ERRORS,
    ]
    return {name: await get(name) for name in names}
