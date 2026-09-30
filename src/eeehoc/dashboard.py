"""Local HTTP dashboard: NHL Live chiclets."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from eeehoc.live import DATE_RE, LiveFeed

STATIC_DIR = Path(__file__).resolve().parent / "static"


class DashboardState:
    def __init__(self, live: LiveFeed | None = None) -> None:
        self.live = live or LiveFeed()


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def make_handler(state: DashboardState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # quieter
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if getattr(self, "_omit_body", False):
                return
            self.wfile.write(body)

        def do_HEAD(self) -> None:  # noqa: N802
            self._omit_body = True
            try:
                self.do_GET()
            finally:
                self._omit_body = False

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/static/app.css":
                return self._send(200, (STATIC_DIR / "app.css").read_bytes(), "text/css; charset=utf-8")
            if path == "/static/app.js":
                return self._send(
                    200, (STATIC_DIR / "app.js").read_bytes(), "application/javascript; charset=utf-8"
                )
            if path == "/health":
                return self._send(200, b"ok\n", "text/plain; charset=utf-8")

            if path == "/api/live":
                raw_dates = (qs.get("dates") or [""])[0].strip()
                dates = raw_dates or None
                if dates is not None and not DATE_RE.fullmatch(dates):
                    return self._send(
                        400, _json_bytes({"error": "dates must be YYYYMMDD", "games": []}), "application/json"
                    )
                force = (qs.get("force") or ["0"])[0] in ("1", "true")
                try:
                    board = state.live.get(force=force, dates=dates)
                except Exception as exc:  # noqa: BLE001 - surface feed outages to the UI
                    return self._send(502, _json_bytes({"error": str(exc), "games": []}), "application/json")
                return self._send(200, _json_bytes(board), "application/json")

            return self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


def serve(*, port: int, host: str = "127.0.0.1", live: LiveFeed | None = None) -> None:
    state = DashboardState(live=live)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
