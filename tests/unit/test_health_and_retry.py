"""Tests for bot/health_server.py and news_fetcher retry logic."""

from __future__ import annotations

import asyncio
import json

import pytest

from newsdrop.bot import health_server


class TestHealthServer:
    """Test the stdlib HTTP health server by hitting it over the wire."""

    def test_health_endpoint_returns_200(self):
        server = health_server.start_health_server(port=18080)
        try:
            import urllib.request

            resp = urllib.request.urlopen("http://localhost:18080/health", timeout=3)
            body = json.loads(resp.read())
            assert resp.status == 200
            assert body["status"] == "ok"
        finally:
            server.shutdown()

    def test_ready_endpoint_returns_200_when_ready(self):
        health_server.set_ready(True)
        server = health_server.start_health_server(port=18081)
        try:
            import urllib.request

            resp = urllib.request.urlopen("http://localhost:18081/ready", timeout=3)
            body = json.loads(resp.read())
            assert resp.status == 200
            assert body["status"] == "ready"
        finally:
            server.shutdown()
            health_server.set_ready(False)

    def test_ready_endpoint_returns_503_when_not_ready(self):
        health_server.set_ready(False)
        server = health_server.start_health_server(port=18082)
        try:
            import urllib.error
            import urllib.request

            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen("http://localhost:18082/ready", timeout=3)
            assert exc_info.value.code == 503
        finally:
            server.shutdown()

    def test_unknown_endpoint_returns_404(self):
        server = health_server.start_health_server(port=18083)
        try:
            import urllib.error
            import urllib.request

            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen("http://localhost:18083/unknown", timeout=3)
            assert exc_info.value.code == 404
        finally:
            server.shutdown()


class TestRetryLogic:
    """Test the 3-attempt retry with exponential backoff in _fetch_news.

    The retry logic wraps the httpx.AsyncClient context manager, so we
    mock at the _fetch_news level by patching the whole function's
    internals: we patch `api_budget_check` and `cache_get`, then patch
    `httpx.AsyncClient` as an async context manager that raises/succeeds.
    """

    def test_retry_on_500_error(self):
        """A 500 error should be retried 3 times before failing."""
        from unittest.mock import AsyncMock, patch

        from newsdrop.news_fetcher import APIClientError, _fetch_news

        call_count = 0

        class _FailingClient:
            async def get(self, *_args, **_kwargs):
                nonlocal call_count
                call_count += 1
                raise APIClientError("Server error", status_code=500)

        async def _run():
            with (
                patch(
                    "newsdrop.news_fetcher.get_http_client",
                    new_callable=AsyncMock,
                    return_value=_FailingClient(),
                ),
                patch(
                    "newsdrop.news_fetcher.api_budget_check",
                    new_callable=AsyncMock,
                    return_value=True,
                ),
                patch(
                    "newsdrop.news_fetcher.cache_get", new_callable=AsyncMock, return_value=None
                ),
                patch("asyncio.sleep", new_callable=AsyncMock),
                pytest.raises(APIClientError),
            ):
                await _fetch_news({"apikey": "x", "country": "us"})

        asyncio.run(_run())
        assert call_count == 3, f"Expected 3 retry attempts, got {call_count}"

    def test_no_retry_on_401_error(self):
        """A 401 (auth) error should NOT be retried — fail immediately."""
        from unittest.mock import AsyncMock, patch

        from newsdrop.news_fetcher import APIClientError, _fetch_news

        call_count = 0

        class _UnauthorizedClient:
            async def get(self, *_args, **_kwargs):
                nonlocal call_count
                call_count += 1
                raise APIClientError("Invalid key", status_code=401)

        async def _run():
            with (
                patch(
                    "newsdrop.news_fetcher.get_http_client",
                    new_callable=AsyncMock,
                    return_value=_UnauthorizedClient(),
                ),
                patch(
                    "newsdrop.news_fetcher.api_budget_check",
                    new_callable=AsyncMock,
                    return_value=True,
                ),
                patch(
                    "newsdrop.news_fetcher.cache_get", new_callable=AsyncMock, return_value=None
                ),
                patch("asyncio.sleep", new_callable=AsyncMock),
                pytest.raises(APIClientError),
            ):
                await _fetch_news({"apikey": "x", "country": "us"})

        asyncio.run(_run())
        assert call_count == 1, f"Expected 1 attempt (no retry for 401), got {call_count}"

    def test_retry_succeeds_on_second_attempt(self):
        """If the first attempt fails with 500 but the second succeeds, return result."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from newsdrop.news_fetcher import APIClientError, _fetch_news

        call_count = 0

        class _FlakyClient:
            """Fails on first call, succeeds on second."""

            async def get(self, *_args, **_kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise APIClientError("Server error", status_code=500)
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.content = b'{"status": "ok", "results": []}'
                mock_response.json.return_value = {"status": "ok", "results": []}
                return mock_response

        async def _run():
            with (
                patch(
                    "newsdrop.news_fetcher.get_http_client",
                    new_callable=AsyncMock,
                    return_value=_FlakyClient(),
                ),
                patch(
                    "newsdrop.news_fetcher.api_budget_check",
                    new_callable=AsyncMock,
                    return_value=True,
                ),
                patch(
                    "newsdrop.news_fetcher.cache_get", new_callable=AsyncMock, return_value=None
                ),
                patch("newsdrop.news_fetcher.api_request_consume", new_callable=AsyncMock),
                patch("asyncio.sleep", new_callable=AsyncMock),
            ):
                result = await _fetch_news({"apikey": "x", "country": "us"})
                assert result is not None

        asyncio.run(_run())
        assert call_count == 2, f"Expected 2 attempts (fail then succeed), got {call_count}"
