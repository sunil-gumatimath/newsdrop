from __future__ import annotations

from newsdrop import metrics


async def test_increment_and_get():
    await metrics.increment(metrics.COMMAND_NEWS, value=3)
    assert await metrics.get(metrics.COMMAND_NEWS) == 3


async def test_all_metrics_returns_known_counters():
    await metrics.increment(metrics.COMMAND_SEARCH, value=5)
    all_values = await metrics.all_metrics()
    assert all_values[metrics.COMMAND_SEARCH] == 5
    assert metrics.COMMAND_TOTAL in all_values
    assert metrics.DAILY_MESSAGES_SENT in all_values
    assert metrics.BREAKING_ALERTS_SENT in all_values
