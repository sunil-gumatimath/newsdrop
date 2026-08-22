"""Additional unit tests for state.py: backends, guards, eviction, WindowedCounter."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from newsdrop import state as state_mod
from newsdrop.state import (
    WindowedCounter,
    _MemoryBackend,
    _RedisBackend,
    _safe_int,
    get_backend,
    reset_backend,
)

# ── _safe_int ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, 5, 5),
        (7, 0, 7),
        ("42", 0, 42),
        ("  42 ", 0, 42),
        ("not-a-number", 9, 9),
        (object(), 3, 3),
        (True, 0, 1),  # bool is an int subclass
    ],
)
def test_safe_int(value, default, expected):
    assert _safe_int(value, default) == expected


# ── in-memory backend ───────────────────────────────────────────────────


async def test_memory_cache_ttl_expiry_removes_entry():
    backend = _MemoryBackend()
    await backend.set_cache("k", "v", ttl_seconds=0)
    await asyncio.sleep(0.01)
    assert await backend.get_cache("k") is None
    # Expired entry was evicted from the dict, not just hidden.
    assert "k" not in backend._cache


async def test_memory_cache_eviction_under_pressure():
    backend = _MemoryBackend()
    # Force the high-water mark instead of inserting 10k entries.
    backend._cache = {f"k{i}": (float(i), i, 10_000) for i in range(10_000)}
    await backend.set_cache("new", "value", ttl_seconds=60)
    # 20% (2000) of the oldest entries are evicted, then the new one is added.
    assert len(backend._cache) == 8001
    assert await backend.get_cache("new") == "value"
    # Oldest (lowest timestamp) entries were evicted.
    assert "k0" not in backend._cache
    assert "k1999" not in backend._cache


async def test_memory_budget_limit_zero_disables_limiting():
    backend = _MemoryBackend()
    assert await backend.check_api_budget(0) is True
    assert await backend.consume_api_request(0) is True
    used, limit = await backend.get_api_request_count(0)
    assert used == 0
    assert limit == 0


async def test_memory_rate_limit_cleanup_of_expired_entries():
    backend = _MemoryBackend()
    loop = asyncio.get_running_loop()
    now = loop.time()
    # Simulate >5000 entries with a mix of expired and live expiries.
    for i in range(5001):
        expiry = now - 100 if i % 2 == 0 else now + 10_000
        backend._rate_limits[f"scope:{i}"] = expiry
    # Key for (scope="scope", chat_id=1) is "scope:1" — a live entry.
    limited = await backend.is_rate_limited("scope", 1, 5)
    assert limited is True
    # Expired even-index entries were cleaned up during the check.
    assert len(backend._rate_limits) < 5001


async def test_memory_try_acquire_cleans_expired_entries():
    backend = _MemoryBackend()
    loop = asyncio.get_running_loop()
    now = loop.time()
    for i in range(5001):
        backend._rate_limits[f"scope:{i}"] = now - 50
    acquired = await backend.try_acquire_rate_limit("fresh", 1, 30)
    assert acquired is True
    assert len(backend._rate_limits) < 5001


async def test_memory_metric_increment_ignores_negative_values():
    backend = _MemoryBackend()
    await backend.metric_increment("m", 5)
    await backend.metric_increment("m", -3)
    assert await backend.metric_get("m") == 5
    assert await backend.metric_get("missing") == 0


async def test_memory_daily_reset_when_date_changes():
    backend = _MemoryBackend()
    await backend.consume_api_request(limit=10)
    assert (await backend.get_api_request_count(10))[0] == 1
    # Pretend the stored day is yesterday.
    from datetime import date, timedelta

    backend._daily_date = date.today() - timedelta(days=1)
    used, _ = await backend.get_api_request_count(10)
    assert used == 0


# ── Redis backend (fake redis object — no server required) ──────────────


class _FakePipeline:
    def __init__(self, store: dict):
        self._store = store
        self._ops: list[tuple[str, tuple]] = []

    def incr(self, key):
        self._ops.append(("incr", (key,)))
        return self

    def expire(self, key, seconds):
        self._ops.append(("expire", (key, seconds)))
        return self

    async def execute(self):
        results = []
        for op, args in self._ops:
            if op == "incr":
                self._store[args[0]] = self._store.get(args[0], 0) + 1
                results.append(self._store[args[0]])
            elif op == "expire":
                results.append(True)
        self._ops.clear()
        return results


class _FakeRedis:
    """Minimal async stand-in for redis.asyncio.Redis."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("simulated redis outage")

    async def get(self, key: str):
        self._check()
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:  # noqa: ARG002
        self._check()
        self.store[key] = value

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,  # noqa: ARG002
    ):
        self._check()
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def exists(self, key: str) -> int:
        self._check()
        return 1 if key in self.store else 0

    async def incrby(self, key: str, amount: int) -> int:
        self._check()
        current = int(self.store.get(key, 0))
        self.store[key] = str(current + amount)
        return current + amount

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self.store)


def _redis_backend_with(fake: _FakeRedis) -> _RedisBackend:
    backend = _RedisBackend.__new__(_RedisBackend)
    backend._redis = fake
    backend._budget_fallback_count = 0
    backend._budget_fallback_date = date.today()
    return backend


async def test_redis_cache_round_trip_and_bad_json():
    fake = _FakeRedis()
    backend = _redis_backend_with(fake)

    await backend.set_cache("k", {"a": 1}, ttl_seconds=60)
    assert await backend.get_cache("k") == {"a": 1}
    assert await backend.get_cache("missing") is None

    fake.store[backend._key("cache", "bad")] = "{not json"
    assert await backend.get_cache("bad") is None


async def test_redis_cache_errors_are_swallowed():
    fake = _FakeRedis()
    fake.fail = True
    backend = _redis_backend_with(fake)
    assert await backend.get_cache("k") is None
    await backend.set_cache("k", "v", ttl_seconds=60)  # must not raise


async def test_redis_budget_guards_and_counting():
    fake = _FakeRedis()
    backend = _redis_backend_with(fake)

    # limit <= 0 disables limiting entirely.
    assert await backend.check_api_budget(0) is True
    assert await backend.consume_api_request(-1) is True

    assert await backend.check_api_budget(2) is True
    assert await backend.consume_api_request(2) is True
    assert await backend.consume_api_request(2) is True
    assert await backend.consume_api_request(2) is False
    assert await backend.check_api_budget(2) is False
    used, limit = await backend.get_api_request_count(2)
    assert used == 3  # three consumes were recorded before the cap refused more
    assert limit == 2


async def test_redis_budget_errors_fail_open():
    fake = _FakeRedis()
    fake.fail = True
    backend = _redis_backend_with(fake)
    assert await backend.check_api_budget(2) is True
    assert await backend.consume_api_request(2) is True
    assert await backend.get_api_request_count(2) == (0, 2)


async def test_redis_rate_limit_paths():
    fake = _FakeRedis()
    backend = _redis_backend_with(fake)

    assert await backend.is_rate_limited("news", 1, 30) is False
    await backend.record_rate_limit("news", 1, 30)
    assert await backend.is_rate_limited("news", 1, 30) is True

    # try_acquire uses SET NX EX semantics.
    assert await backend.try_acquire_rate_limit("search", 2, 30) is True
    assert await backend.try_acquire_rate_limit("search", 2, 30) is False

    # cooldown <= 0 short-circuits.
    await backend.record_rate_limit("news", 3, 0)
    assert await backend.try_acquire_rate_limit("news", 4, 0) is True


async def test_redis_rate_limit_errors_fail_open():
    fake = _FakeRedis()
    fake.fail = True
    backend = _redis_backend_with(fake)
    assert await backend.is_rate_limited("s", 1, 10) is False
    await backend.record_rate_limit("s", 1, 10)  # must not raise
    assert await backend.try_acquire_rate_limit("s", 1, 10) is True


async def test_redis_metrics_counters():
    fake = _FakeRedis()
    backend = _redis_backend_with(fake)
    await backend.metric_increment("errors", 3)
    await backend.metric_increment("errors")
    assert await backend.metric_get("errors") == 4
    assert await backend.metric_get("unknown") == 0

    fake.fail = True
    await backend.metric_increment("errors")  # must not raise
    assert await backend.metric_get("errors") == 0


def test_redis_backend_rejects_missing_redis_package(monkeypatch):
    monkeypatch.setattr(state_mod, "Redis", None)
    with pytest.raises(RuntimeError, match="redis package"):
        _RedisBackend("redis://localhost:6379/0")


def test_create_backend_selects_redis_when_url_set(monkeypatch):
    fake_cls = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(state_mod, "REDIS_URL", "redis://localhost:6379/0", raising=False)
    monkeypatch.setattr(state_mod, "Redis", fake_cls)
    reset_backend()
    backend = get_backend()
    assert isinstance(backend, _RedisBackend)
    fake_cls.from_url.assert_called_once()


# ── WindowedCounter ─────────────────────────────────────────────────────


async def test_windowed_counter_increment_and_windows():
    counter = WindowedCounter("test", windows=(3600, 86400))
    await counter.increment(3)
    await counter.increment()
    assert await counter.count_window(3600) == 4
    assert await counter.count_window(86400) == 4
    assert await counter.count_window(0) == 0


async def test_windowed_counter_trims_old_events():
    counter = WindowedCounter("test", windows=(3600,))
    loop = asyncio.get_running_loop()
    now = loop.time()
    # Inject one ancient event and one fresh event.
    counter._events = [now - 7200, now]
    trimmed = await counter.count_window(3600)
    assert trimmed == 1
    assert len(counter._events) == 1


async def test_windowed_counter_zero_value_is_noop():
    counter = WindowedCounter("test", windows=(3600,))
    await counter.increment(0)
    assert await counter.count_window(3600) == 0


# ── module-level convenience wrappers ───────────────────────────────────


async def test_metric_wrappers_use_active_backend():
    reset_backend()
    await state_mod.metric_increment("wrapped", 2)
    assert await state_mod.metric_get("wrapped") == 2


async def test_json_serialization_default_str_for_objects():
    from datetime import datetime

    backend = _MemoryBackend()
    await backend.set_cache("obj", {"when": datetime(2025, 1, 1)}, ttl_seconds=60)
    raw = json.dumps({"ok": True})
    assert isinstance(raw, str)
