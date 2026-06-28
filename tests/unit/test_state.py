from __future__ import annotations

import asyncio

import pytest

from newsdrop.state import (
    api_budget_check,
    api_request_consume,
    api_request_count,
    cache_get,
    cache_set,
    get_backend,
    rate_limit_check,
    rate_limit_record,
    reset_backend,
)


@pytest.fixture(autouse=True)
def _fresh_backend():
    reset_backend()
    yield
    reset_backend()


async def test_cache_round_trip():
    await cache_set("key", {"value": 42}, ttl_seconds=300)
    cached = await cache_get("key")
    assert cached == {"value": 42}


async def test_cache_expires():
    await cache_set("key", {"value": 42}, ttl_seconds=0)
    await asyncio.sleep(0.01)
    cached = await cache_get("key")
    assert cached is None


async def test_api_budget_and_consume():
    assert await api_budget_check(limit=2) is True
    assert await api_request_consume(limit=2) is True
    count, limit = await api_request_count(limit=2)
    assert count == 1
    assert limit == 2
    assert await api_request_consume(limit=2) is True
    assert await api_request_consume(limit=2) is False
    assert await api_budget_check(limit=2) is False


async def test_rate_limit_blocks_and_expires():
    chat_id = 12345
    assert await rate_limit_check("test", chat_id, cooldown_seconds=1) is False
    await rate_limit_record("test", chat_id, cooldown_seconds=1)
    assert await rate_limit_check("test", chat_id, cooldown_seconds=1) is True
    await asyncio.sleep(1.1)
    assert await rate_limit_check("test", chat_id, cooldown_seconds=1) is False


async def test_backend_reset_clears_state():
    await cache_set("key", "value", ttl_seconds=300)
    await api_request_consume(limit=10)
    await rate_limit_record("scope", 1, cooldown_seconds=300)

    reset_backend()
    backend = get_backend()
    assert await backend.get_cache("key") is None
    count, _ = await api_request_count(limit=10)
    assert count == 0
    assert await rate_limit_check("scope", 1, cooldown_seconds=300) is False
