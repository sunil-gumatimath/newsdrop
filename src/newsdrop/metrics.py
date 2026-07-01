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

# Windowed counters for rate-based alerting (sliding windows).
_windowed_names = [
    "rate:news_api_errors",
    "rate:unexpected_errors",
    "rate:daily_messages_sent",
    "rate:breaking_alerts_sent",
    "rate:command_total",
]

_windowed_counters: dict[str, WindowedCounter] = {}


def _get_windowed(name: str) -> WindowedCounter:
    if name not in _windowed_counters:
        _windowed_counters[name] = WindowedCounter(name, windows=(3600, 86400))
    return _windowed_counters[name]


async def increment(name: str, value: int = 1) -> None:
    """Increment a counter by ``value``."""
    await _metric_increment(name, value)
    # Track rate windows for error and high-volume metrics.
    rate_key = f"rate:{name}"
    if rate_key in _windowed_names:
        await _get_windowed(rate_key).increment(value)


async def get(name: str) -> int:
    """Return the current value of a counter."""
    return await _metric_get(name)


async def get_rate(name: str, window_seconds: int = 3600) -> int:
    """Return the number of increments in the given sliding window.

    window_seconds must be one of 3600 (1h) or 86400 (24h).
    """
    rate_key = f"rate:{name}"
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
