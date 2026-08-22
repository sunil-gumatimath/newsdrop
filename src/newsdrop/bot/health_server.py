"""Minimal HTTP health server for Docker/container orchestration.

Runs on a background thread inside the bot process and exposes:
  /health  → 200 {"status": "ok"} when the bot is running
  /ready  → 200 once the Application has been built, 503 before
  /metrics → 200 with all named counters + sliding-window rates as JSON

This is intentionally dependency-free (stdlib only) so the bot does not
need extra packages just to answer a healthcheck.
"""

from __future__ import annotations

import asyncio
import atexit
import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)

try:
    HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))
    HEALTH_PORT = max(1, min(65535, HEALTH_PORT))
except (TypeError, ValueError):
    HEALTH_PORT = 8080

# Bind address for the health server.
# SECURITY: 0.0.0.0 exposes /metrics (counters, rates) and /health to the
# network. Default to 127.0.0.1 (loopback) so metrics are not exposed
# beyond the host/container unless explicitly requested via HEALTH_BIND.
# Set HEALTH_BIND=0.0.0.0 only behind a trusted reverse proxy / firewall.
HEALTH_BIND = os.getenv("HEALTH_BIND", "127.0.0.1").strip() or "127.0.0.1"

# Optional bearer-token auth for health/metrics endpoints.
# If HEALTH_TOKEN is set, every request must include:
#   Authorization: Bearer <HEALTH_TOKEN>
# (exact match, constant-time comparison). When unset, endpoints are
# unauthenticated but still bound to HEALTH_BIND.
HEALTH_TOKEN = os.getenv("HEALTH_TOKEN", "").strip()

_ready = False
# Reusable event loop for the health server so we don't create/destroy
# a loop on every /metrics request (which would leak resources).
_metrics_loop: asyncio.AbstractEventLoop | None = None
_metrics_loop_lock = threading.Lock()


def set_ready(value: bool) -> None:
    global _ready
    _ready = value


def _get_metrics_loop() -> asyncio.AbstractEventLoop:
    """Return the shared event loop for metrics queries, creating it once."""
    global _metrics_loop
    if _metrics_loop is not None and not _metrics_loop.is_closed():
        return _metrics_loop
    with _metrics_loop_lock:
        if _metrics_loop is None or _metrics_loop.is_closed():
            _metrics_loop = asyncio.new_event_loop()
        return _metrics_loop


def _close_metrics_loop() -> None:
    """Close and discard the shared metrics event loop (atexit / tests)."""
    global _metrics_loop
    with _metrics_loop_lock:
        if _metrics_loop is not None:
            try:
                if not _metrics_loop.is_closed():
                    _metrics_loop.close()
            except Exception:
                pass
            _metrics_loop = None


atexit.register(_close_metrics_loop)


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        logger.debug("health server: " + (format % args))

    def _is_authorized(self) -> bool:
        """Check Authorization header when HEALTH_TOKEN is set."""
        if not HEALTH_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        # Accept "Bearer <token>" or raw token value.
        expected_bearer = f"Bearer {HEALTH_TOKEN}"
        # Constant-time comparison to avoid timing side-channel.
        return hmac.compare_digest(auth, expected_bearer) or hmac.compare_digest(auth, HEALTH_TOKEN)

    def do_GET(self) -> None:
        if HEALTH_TOKEN and not self._is_authorized():
            self._respond(401, {"error": "unauthorized"})
            return
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        elif self.path == "/ready":
            if _ready:
                self._respond(200, {"status": "ready"})
            else:
                self._respond(503, {"status": "starting"})
        elif self.path == "/metrics":
            self._handle_metrics()
        else:
            self._respond(404, {"error": "not found"})

    def _handle_metrics(self) -> None:
        from ..metrics import all_metrics, get_rate

        loop = _get_metrics_loop()
        try:
            metrics = loop.run_until_complete(all_metrics())
            rates: dict[str, int] = {}
            rate_keys = [
                "news_api_errors",
                "unexpected_errors",
                "daily_messages_sent",
                "breaking_alerts_sent",
                "command_total",
            ]
            for rk in rate_keys:
                rates[f"{rk}_1h"] = loop.run_until_complete(get_rate(rk, 3600))
                rates[f"{rk}_24h"] = loop.run_until_complete(get_rate(rk, 86400))
        except Exception:
            logger.exception("Failed to fetch metrics")
            self._respond(500, {"status": "error", "error": "metrics fetch failed"})
            return
        self._respond(
            200,
            {"status": "ok", "metrics": metrics, "rates": rates},
        )

    def _respond(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected — not our problem.

    # ═══════════════════════════════════════════════════════════════════
    # Override ``BaseHTTPRequestHandler.address_string()`` to avoid
    # expensive DNS reverse-lookup that can block the health server
    # thread for seconds (especially in Docker/GCE).
    # ═══════════════════════════════════════════════════════════════════
    def address_string(self) -> str:
        return str(self.client_address[0])


def start_health_server(port: int = HEALTH_PORT, bind: str | None = None) -> HTTPServer:
    """Start the health server in a daemon thread and return the server instance.

    Args:
        port: TCP port to listen on.
        bind: Interface to bind. Defaults to HEALTH_BIND env (127.0.0.1).
              Use 0.0.0.0 only behind a trusted firewall/reverse proxy —
              it exposes /metrics to the network.
    """
    host = bind if bind is not None else HEALTH_BIND
    # Normalize empty string to loopback.
    if not host.strip():
        host = "127.0.0.1"
    server = HTTPServer((host, port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    thread.start()
    # Log whether auth is active so operators can verify.
    auth_note = " (auth enabled)" if HEALTH_TOKEN else ""
    logger.info("Health server listening on http://%s:%d/health%s", host, port, auth_note)
    return server
