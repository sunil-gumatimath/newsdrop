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
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)

HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))

_ready = False


def set_ready(value: bool) -> None:
    global _ready
    _ready = value


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        logger.debug("health server: " + (format % args))

    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        elif self.path == "/ready":
            if _ready:
                self._respond(200, {"status": "ready"})
            else:
                self._respond(503, {"status": "starting"})
        elif self.path == "/metrics":
            from ..metrics import all_metrics, get_rate

            loop = asyncio.new_event_loop()
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
            finally:
                loop.close()
            self._respond(
                200,
                {"status": "ok", "metrics": metrics, "rates": rates},
            )
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_health_server(port: int = HEALTH_PORT) -> HTTPServer:
    """Start the health server in a daemon thread and return the server instance."""
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    thread.start()
    logger.info("Health server listening on http://0.0.0.0:%d/health", port)
    return server
