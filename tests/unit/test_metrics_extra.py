"""Additional unit tests for metrics.py: windowed rate keys, rates, all_metrics."""

from __future__ import annotations

import pytest

from newsdrop import metrics


@pytest.fixture(autouse=True)
def _fresh_windowed_counters():
    """Isolate sliding-window state from other tests (module-level dict)."""
    metrics._windowed_counters.clear()
    yield
    metrics._windowed_counters.clear()


# ── _windowed_rate_key ──────────────────────────────────────────────────


def test_windowed_rate_key_accepts_canonical_constant():
    assert metrics._windowed_rate_key(metrics.NEWS_API_ERRORS) == f"rate:{metrics.NEWS_API_ERRORS}"
    assert (
        metrics._windowed_rate_key(metrics.BREAKING_ALERTS_SENT)
        == f"rate:{metrics.BREAKING_ALERTS_SENT}"
    )


def test_windowed_rate_key_accepts_short_base_name():
    assert metrics._windowed_rate_key("news_api_errors") == f"rate:{metrics.NEWS_API_ERRORS}"
    assert metrics._windowed_rate_key("unexpected_errors") == f"rate:{metrics.UNEXPECTED_ERRORS}"
    assert (
        metrics._windowed_rate_key("daily_messages_sent") == f"rate:{metrics.DAILY_MESSAGES_SENT}"
    )
    assert (
        metrics._windowed_rate_key("breaking_alerts_sent") == f"rate:{metrics.BREAKING_ALERTS_SENT}"
    )
    assert metrics._windowed_rate_key("command_total") == f"rate:{metrics.COMMAND_TOTAL}"


def test_windowed_rate_key_unknown_name_returns_none():
    assert metrics._windowed_rate_key(metrics.COMMAND_NEWS) is None
    assert metrics._windowed_rate_key("totally-unknown") is None
    assert metrics._windowed_rate_key("") is None


# ── increment / get / get_rate integration ──────────────────────────────


async def test_increment_updates_sliding_windows():
    # The short base name and the canonical constant share one rate window,
    # but they are separate named counters.
    await metrics.increment("news_api_errors", value=2)
    await metrics.increment(metrics.NEWS_API_ERRORS)

    assert await metrics.get(metrics.NEWS_API_ERRORS) == 1
    assert await metrics.get("news_api_errors") == 2

    # Both name forms read the same sliding window.
    assert await metrics.get_rate("news_api_errors", 3600) == 3
    assert await metrics.get_rate(metrics.NEWS_API_ERRORS, 86400) == 3


async def test_get_rate_zero_for_non_windowed_metric():
    await metrics.increment(metrics.COMMAND_NEWS, value=4)
    assert await metrics.get_rate(metrics.COMMAND_NEWS, 3600) == 0
    assert await metrics.get_rate("unknown-metric", 3600) == 0


async def test_get_rate_default_window_is_one_hour():
    await metrics.increment("command_total", value=5)
    assert await metrics.get_rate("command_total") == 5


async def test_get_returns_zero_for_unknown_counter():
    assert await metrics.get("never:incremented") == 0


async def test_all_metrics_lists_every_known_counter():
    snapshot = await metrics.all_metrics()
    expected = {
        metrics.COMMAND_TOTAL,
        metrics.COMMAND_NEWS,
        metrics.COMMAND_SEARCH,
        metrics.COMMAND_FOLLOW,
        metrics.COMMAND_UNFOLLOW,
        metrics.COMMAND_SUBSCRIBE,
        metrics.COMMAND_UNSUBSCRIBE,
        metrics.COMMAND_BREAKING_TOGGLE,
        metrics.COMMAND_TRENDING,
        metrics.COMMAND_HEALTH,
        metrics.DAILY_MESSAGES_SENT,
        metrics.BREAKING_ALERTS_SENT,
        metrics.NEWS_API_ERRORS,
        metrics.UNEXPECTED_ERRORS,
    }
    assert set(snapshot) == expected
    assert all(isinstance(v, int) for v in snapshot.values())
