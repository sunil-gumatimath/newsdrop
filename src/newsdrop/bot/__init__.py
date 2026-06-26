from __future__ import annotations

# Expose all public APIs to maintain backward compatibility with external scripts and tests.
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
)
from .jobs import (
    send_breaking_news_alerts,
    send_daily_news,
)
from .main import (
    error_handler,
    main,
)

__all__ = [
    "button_handler",
    "breaking_toggle",
    "clear_chat",
    "follow_topic",
    "health",
    "help_command",
    "list_followed_topics",
    "news",
    "preferences",
    "search",
    "set_category",
    "set_country",
    "start",
    "subscribe",
    "trending",
    "unfollow_all_topics",
    "unfollow_topic",
    "send_breaking_news_alerts",
    "send_daily_news",
    "error_handler",
    "main",
]
