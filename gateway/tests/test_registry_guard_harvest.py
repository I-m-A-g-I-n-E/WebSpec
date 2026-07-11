"""Guard-aware catalog harvesting: services that guard OPTIONS / with a
401/403 must be inventoried by retrying with a nonce-bootstrapped guard
HMAC, using the *same* webspec.guard.compute_guard_hmac the gateway itself
uses. This exercises that flow end-to-end against a tiny deterministic
stub HTTP server (no real gateway involved).
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from webspec.guard import compute_guard_hmac
from webspec_registry.catalog import http_fetch_tools

KEY = b"\x42" * 32
WRONG_KEY = b"\x24" * 32
HOST = "guarded-svc.localhost"
FIXED_NONCE = "deadbeefcafef00d"

TOOLS_PAYLOAD = {
    "service": "guarded-svc",
    "tools": [{"name": "secret_tool", "description": "x", "inputSchema": {}}],
}


class _GuardedHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence test output
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/__nonce":
            self._send_json(404, {"error": "not_found"})
            return
        host = self.headers.get("Host", "")
        guard_header = self.headers.get("X-WebSpec-Guard", "")
        expected = compute_guard_hmac(KEY, "GET", host, "/__nonce", "", b"")
        if guard_header.lower() != expected.lower():
            self._send_json(401, {"error": "guard_invalid"})
            return
        self._send_json(200, {"nonce": FIXED_NONCE, "audience": "guarded-svc"})

    def do_OPTIONS(self):
        if self.path != "/":
            self._send_json(404, {"error": "not_found"})
            return
        host = self.headers.get("Host", "")
        guard_header = self.headers.get("X-WebSpec-Guard")
        nonce_header = self.headers.get("X-WebSpec-Nonce")
        if not guard_header or not nonce_header:
            self._send_json(401, {"error": "guard_missing"})
            return
        expected = compute_guard_hmac(KEY, "OPTIONS", host, "/", nonce_header, b"")
        if guard_header.lower() != expected.lower():
            self._send_json(401, {"error": "guard_invalid"})
            return
        self._send_json(200, TOOLS_PAYLOAD)


@pytest.fixture()
def stub_gateway():
    server = HTTPServer(("127.0.0.1", 0), _GuardedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_guarded_service_harvested_with_correct_key(stub_gateway):
    fetch = http_fetch_tools(stub_gateway, guard_key=KEY)
    tools = fetch("guarded-svc")
    assert tools == TOOLS_PAYLOAD["tools"]


def test_unauthenticated_fetch_still_blocked(stub_gateway):
    fetch = http_fetch_tools(stub_gateway)  # no guard_key -> no fallback attempted
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        fetch("guarded-svc")
    assert exc_info.value.code == 401


def test_wrong_key_degrades_cleanly(stub_gateway):
    fetch = http_fetch_tools(stub_gateway, guard_key=WRONG_KEY)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        fetch("guarded-svc")
    assert exc_info.value.code == 401


def test_harvest_skips_guarded_service_when_unauthenticated(stub_gateway):
    from webspec_registry.catalog import harvest

    recs = harvest(["guarded-svc"], http_fetch_tools(stub_gateway))
    assert recs == []


def test_harvest_includes_guarded_service_with_key(stub_gateway):
    from webspec_registry.catalog import harvest

    recs = harvest(["guarded-svc"], http_fetch_tools(stub_gateway, guard_key=KEY))
    assert [r.tool for r in recs] == ["secret_tool"]


def test_harvest_skips_guarded_service_with_wrong_key(stub_gateway):
    from webspec_registry.catalog import harvest

    recs = harvest(["guarded-svc"], http_fetch_tools(stub_gateway, guard_key=WRONG_KEY))
    assert recs == []
