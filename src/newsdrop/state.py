"""Shared state abstraction for cache, API budget, and rate limits.

Supports Redis for multi-worker deployments and an in-memory fallback for
single-worker / local deployments. The backend is selected by the
``REDIS_URL`` environment variable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "")

StateValue = Any


try:
    from redis.asyncio import Redis
except ImportError:  # pragma: no cover
    Redis = None  # type: ignore[misc, assignment]


def _safe_int(value: object, default: int = 0) -> int:
    """Convert value to int, returning ``default`` on failure."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError, OverflowError):
        return default


class StateBackend(ABC):
    """Abstract backend for shared state."""

    @abstractmethod
    async def get_cache(self, key: str) -> StateValue | None:
        """Return cached value or ``None`` if missing/expired."""

    @abstractmethod
    async def set_cache(self, key: str, value: StateValue, ttl_seconds: int) -> None:
        """Store ``value`` with the given TTL."""

    @abstractmethod
    async def check_api_budget(self, limit: int) -> bool:
        """Return ``True`` if another API request is allowed today."""

    @abstractmethod
    async def consume_api_request(self, limit: int) -> bool:
        """Record an API request and return ``True`` if it was within budget."""

    @abstractmethod
    async def get_api_request_count(self, limit: int) -> tuple[int, int]:
        """Return ``(used_today, limit)``."""

    @abstractmethod
    async def is_rate_limited(self, scope: str, chat_id: int, cooldown_seconds: int) -> bool:
        """Return ``True`` if ``chat_id`` is still on cooldown in ``scope``."""

    @abstractmethod
    async def record_rate_limit(self, scope: str, chat_id: int, cooldown_seconds: int) -> None:
        """Mark ``chat_id`` as rate-limited in ``scope`` for ``cooldown_seconds``."""

    @abstractmethod
    async def try_acquire_rate_limit(self, scope: str, chat_id: int, cooldown_seconds: int) -> bool:
        """Atomically check AND record a rate-limit.

        Returns ``True`` if the caller is allowed (was not rate-limited and
        has been marked). ``False`` if the user is still on cooldown.

        Combines ``is_rate_limited`` + ``record_rate_limit`` into one
        atomic call so there is no TOCTOU race between the check and
        the record.
        """

    @abstractmethod
    async def metric_increment(self, name: str, value: int = 1) -> None:
        """Increment a counter metric by ``value``."""

    @abstractmethod
    async def metric_get(self, name: str) -> int:
        """Return the current value of a counter metric."""


class _MemoryBackend(StateBackend):
    """In-memory fallback when Redis is not configured."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[datetime, StateValue, int]] = {}
        self._daily_count = 0
        self._daily_date = date.today()
        self._rate_limits: dict[str, float] = {}
        self._metrics: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(*parts: str) -> str:
        return ":".join(parts)

    def _reset_day_if_needed(self) -> None:
        today = date.today()
        if today != self._daily_date:
            self._daily_count = 0
            self._daily_date = today

    async def get_cache(self, key: str) -> StateValue | None:
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            ts, value, ttl = entry
            if (datetime.now() - ts).total_seconds() >= ttl:
                del self._cache[key]
                return None
            return value

    async def set_cache(self, key: str, value: StateValue, ttl_seconds: int) -> None:
        async with self._lock:
            # Evict oldest 20% when cache exceeds 10,000 keys.
            if len(self._cache) >= 10_000 and key not in self._cache:
                sorted_keys = sorted(self._cache, key=lambda k: self._cache[k][0])
                evict_count = max(1, len(sorted_keys) // 5)
                for old_key in sorted_keys[:evict_count]:
                    del self._cache[old_key]
                logger.info("Cache eviction: removed %d entries", evict_count)
            self._cache[key] = (datetime.now(), value, ttl_seconds)

    async def check_api_budget(self, limit: int) -> bool:
        async with self._lock:
            self._reset_day_if_needed()
            return self._daily_count < limit

    async def consume_api_request(self, limit: int) -> bool:
        async with self._lock:
            self._reset_day_if_needed()
            if self._daily_count >= limit:
                return False
            self._daily_count += 1
            return True

    async def get_api_request_count(self, limit: int) -> tuple[int, int]:
        async with self._lock:
            self._reset_day_if_needed()
            return self._daily_count, limit

    async def is_rate_limited(self, scope: str, chat_id: int, cooldown_seconds: int) -> bool:
        async with self._lock:
            # Periodic cleanup of expired entries (amortized on each check).
            if len(self._rate_limits) > 5000:
                loop = asyncio.get_running_loop()
                now = loop.time()
                expired = [
                    k for k, ts in self._rate_limits.items() if (now - ts) >= cooldown_seconds
                ]
                for k in expired:
                    del self._rate_limits[k]
                if expired:
                    logger.info("Rate-limit cleanup: removed %d expired entries", len(expired))
            last_call = self._rate_limits.get(self._key(scope, str(chat_id)), 0.0)
            loop = asyncio.get_running_loop()
            return (loop.time() - last_call) < cooldown_seconds

    async def record_rate_limit(
        self,
        scope: str,
        chat_id: int,
        _cooldown_seconds: int,
    ) -> None:
        async with self._lock:
            self._rate_limits[self._key(scope, str(chat_id))] = asyncio.get_running_loop().time()

    async def try_acquire_rate_limit(self, scope: str, chat_id: int, cooldown_seconds: int) -> bool:
        """Atomically check + record under the same lock — no TOCTOU race.

        The check and the record must happen while holding ``self._lock``
        (a non-reentrant ``asyncio.Lock``). The earlier implementation
        re-entered ``self.is_rate_limited`` / ``self.record_rate_limit``
        which each acquire the same lock, deadlocking the calling task.
        We inline the logic here so the lock is acquired exactly once.
        """
        async with self._lock:
            # Periodic cleanup of expired entries (amortized on each call).
            if len(self._rate_limits) > 5000:
                now = asyncio.get_running_loop().time()
                expired = [
                    k for k, ts in self._rate_limits.items() if (now - ts) >= cooldown_seconds
                ]
                for k in expired:
                    del self._rate_limits[k]
                if expired:
                    logger.info("Rate-limit cleanup: removed %d expired entries", len(expired))
            last_call = self._rate_limits.get(self._key(scope, str(chat_id)), 0.0)
            now = asyncio.get_running_loop().time()
            if (now - last_call) < cooldown_seconds:
                # Still on cooldown — not acquired.
                return False
            self._rate_limits[self._key(scope, str(chat_id))] = now
            return True

    async def metric_increment(self, name: str, value: int = 1) -> None:
        async with self._lock:
            self._metrics[name] = self._metrics.get(name, 0) + max(value, 0)

    async def metric_get(self, name: str) -> int:
        async with self._lock:
            return self._metrics.get(name, 0)


class _RedisBackend(StateBackend):
    """Redis backend for shared state across workers."""

    def __init__(self, url: str) -> None:
        if Redis is None:
            raise RuntimeError(
                "redis package is required when REDIS_URL is set; install with pip install redis"
            )
        self._redis = Redis.from_url(url, decode_responses=True, socket_timeout=5)

    @staticmethod
    def _key(*parts: str) -> str:
        return ":".join(["newsdrop", *parts])

    def _today(self) -> str:
        return date.today().isoformat()

    async def get_cache(self, key: str) -> StateValue | None:
        try:
            raw = await self._redis.get(self._key("cache", key))
        except Exception:
            logger.exception("Redis get_cache error")
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode cached value: %s", exc)
            return None

    async def set_cache(self, key: str, value: StateValue, ttl_seconds: int) -> None:
        try:
            await self._redis.setex(
                self._key("cache", key),
                ttl_seconds,
                json.dumps(value, default=str),
            )
        except Exception:
            logger.exception("Redis set_cache error")

    async def check_api_budget(self, limit: int) -> bool:
        try:
            count = _safe_int(await self._redis.get(self._key("daily_requests", self._today())))
        except Exception:
            logger.exception("Redis check_api_budget error")
            return True  # allow on error rather than blocking
        return count < limit

    async def consume_api_request(self, limit: int) -> bool:
        try:
            key = self._key("daily_requests", self._today())
            pipe = self._redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, 172800)  # 2 days
            results = await pipe.execute()
            count = _safe_int(results[0])
            return count <= limit
        except Exception:
            logger.exception("Redis consume_api_request error")
            return True  # allow on error rather than blocking

    async def get_api_request_count(self, limit: int) -> tuple[int, int]:
        try:
            count = _safe_int(await self._redis.get(self._key("daily_requests", self._today())))
        except Exception:
            logger.exception("Redis get_api_request_count error")
            count = 0
        return count, limit

    async def is_rate_limited(self, scope: str, chat_id: int, _cooldown_seconds: int) -> bool:
        try:
            exists = await self._redis.exists(self._key("rate_limit", scope, str(chat_id)))
            return bool(exists)
        except Exception:
            logger.exception("Redis is_rate_limited error")
            return False  # allow on error

    async def record_rate_limit(self, scope: str, chat_id: int, cooldown_seconds: int) -> None:
        try:
            await self._redis.setex(
                self._key("rate_limit", scope, str(chat_id)),
                cooldown_seconds,
                "1",
            )
        except Exception:
            logger.exception("Redis record_rate_limit error")

    async def try_acquire_rate_limit(self, scope: str, chat_id: int, cooldown_seconds: int) -> bool:
        """Atomically check + record using Redis ``setnx`` with expiry.

        Uses ``SET key value NX EX cooldown_seconds`` which only sets the
        key if it does **not** already exist — this is the atomic equivalent
        of check-then-set.
        """
        try:
            result = await self._redis.set(
                self._key("rate_limit", scope, str(chat_id)),
                "1",
                nx=True,
                ex=cooldown_seconds,
            )
            return result is not None
        except Exception:
            logger.exception("Redis try_acquire_rate_limit error")
            return True  # allow on error

    async def metric_increment(self, name: str, value: int = 1) -> None:
        try:
            await self._redis.incrby(self._key("metric", name), max(value, 0))
        except Exception:
            logger.exception("Redis metric_increment error")

    async def metric_get(self, name: str) -> int:
        try:
            raw = await self._redis.get(self._key("metric", name))
            return _safe_int(raw)
        except Exception:
            logger.exception("Redis metric_get error")
            return 0


_backend: StateBackend | None = None


def _create_backend() -> StateBackend:
    if REDIS_URL:
        logger.info("Using Redis shared state backend")
        return _RedisBackend(REDIS_URL)
    logger.info("Using in-memory state backend")
    return _MemoryBackend()


def get_backend() -> StateBackend:
    """Return the shared state backend singleton."""
    global _backend
    if _backend is None:
        _backend = _create_backend()
    return _backend


def set_backend(backend: StateBackend) -> None:
    """Override the shared backend. Useful for tests."""
    global _backend
    _backend = backend


def reset_backend() -> None:
    """Reset the backend singleton. Useful for tests."""
    global _backend
    _backend = None


class WindowedCounter:
    """Sliding-window event counter for rate-based metrics.

    Tracks timestamps of increments so callers can query "how many events
    in the last N seconds" without a separate storage backend.

    Used in-memory only (one process); the timestamps live in a list that
    is trimmed on each call. For Redis-backed windowed metrics you would
    use a sorted set — but for metrics (approximate, local-only) a list is
    simpler and fast enough for realistic event rates (< 10k/min).
    """

    def __init__(self, name: str, windows: tuple[int, ...] = (3600, 86400)) -> None:
        self.name = name
        self._windows = windows
        self._max_window = max(windows)
        self._events: list[float] = []
        self._lock = asyncio.Lock()

    async def increment(self, value: int = 1) -> None:
        now = asyncio.get_running_loop().time()
        async with self._lock:
            for _ in range(value):
                self._events.append(now)
            self._trim(now)

    async def count_window(self, window_seconds: int) -> int:
        now = asyncio.get_running_loop().time()
        cutoff = now - window_seconds
        async with self._lock:
            self._trim(now)
            return sum(1 for ts in self._events if ts >= cutoff)

    def _trim(self, now: float) -> None:
        cutoff = now - self._max_window
        self._events = [ts for ts in self._events if ts >= cutoff]


# Convenience wrappers so callers don't need to import get_backend().


async def cache_get(key: str) -> StateValue | None:
    return await get_backend().get_cache(key)


async def cache_set(key: str, value: StateValue, ttl_seconds: int) -> None:
    await get_backend().set_cache(key, value, ttl_seconds)


async def api_budget_check(limit: int) -> bool:
    return await get_backend().check_api_budget(limit)


async def api_request_consume(limit: int) -> bool:
    return await get_backend().consume_api_request(limit)


async def api_request_count(limit: int) -> tuple[int, int]:
    return await get_backend().get_api_request_count(limit)


async def rate_limit_check(scope: str, chat_id: int, cooldown_seconds: int) -> bool:
    return await get_backend().is_rate_limited(scope, chat_id, cooldown_seconds)


async def rate_limit_record(scope: str, chat_id: int, cooldown_seconds: int) -> None:
    await get_backend().record_rate_limit(scope, chat_id, cooldown_seconds)


async def rate_limit_try_acquire(scope: str, chat_id: int, cooldown_seconds: int) -> bool:
    """Atomically check and record a rate-limit.

    Preferred over separate ``rate_limit_check`` + ``rate_limit_record``
    calls to avoid TOCTOU race conditions.
    """
    return await get_backend().try_acquire_rate_limit(scope, chat_id, cooldown_seconds)


async def metric_increment(name: str, value: int = 1) -> None:
    await get_backend().metric_increment(name, value)


async def metric_get(name: str) -> int:
    return await get_backend().metric_get(name)
