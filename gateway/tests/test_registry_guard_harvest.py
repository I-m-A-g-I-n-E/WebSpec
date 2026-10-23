"""Guard-aware catalog harvesting: services that guard GET / and OPTIONS / with a
401/403 must be inventoried by retrying with a nonce-bootstrapped guard
HMAC, using the *same* webspec.guard.compute_guard_hmac the gateway itself
uses. This exercises that flow end-to-end against a tiny deterministic
stub HTTP server, and the harvest itself against the real gateway app.

The harvest reads v2 discovery: the tools and their descriptions from GET /
(DS-3), and each tool's inputSchema from the OPTIONS / entries (DS-1).
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

from tests._gateway import KEY as GATEWAY_KEY, entry, make_client, tool

KEY = b"\x42" * 32
WRONG_KEY = b"\x24" * 32
HOST = "guarded-svc.localhost"
FIXED_NONCE = "deadbeefcafef00d"
SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}}

# What a v2 gateway answers on GET / and OPTIONS / for a destination with one tool.
LISTING = {"service": "guarded-svc", "level": 1, "tools": [
    {"name": "secret_tool", "description": "x", "method": "POST", "path": "/secret_tool", "definer": "INVOKE"}]}
ENTRIES = {"service": "guarded-svc", "level": 1, "entries": [
    {"path": "/secret_tool", "method": "POST", "definer": "INVOKE", "tool": "secret_tool", "inputSchema": SCHEMA,
     "contract": {}, "requirements": {}}]}
HARVESTED = [{"name": "secret_tool", "description": "x", "inputSchema": SCHEMA}]


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

    def _guarded(self, payload: dict) -> None:
        host = self.headers.get("Host", "")
        guard_header = self.headers.get("WebSpec-Guard")
        nonce_header = self.headers.get("WebSpec-Nonce")
        if not guard_header or not nonce_header:
            self._send_json(401, {"error": "guard_missing"})
            return
        expected = compute_guard_hmac(KEY, self.command, host, "/", nonce_header, b"")
        if guard_header.lower() != expected.lower():
            self._send_json(401, {"error": "guard_invalid"})
            return
        self._send_json(200, payload)

    def do_GET(self):
        if self.path == "/":
            self._guarded(LISTING)
            return
        if self.path != "/__nonce":
            self._send_json(404, {"error": "not_found"})
            return
        host = self.headers.get("Host", "")
        guard_header = self.headers.get("WebSpec-Guard", "")
        expected = compute_guard_hmac(KEY, "GET", host, "/__nonce", "", b"")
        if guard_header.lower() != expected.lower():
            self._send_json(401, {"error": "guard_invalid"})
            return
        self._send_json(200, {"nonce": FIXED_NONCE, "audience": "guarded-svc"})

    def do_OPTIONS(self):
        if self.path != "/":
            self._send_json(404, {"error": "not_found"})
            return
        self._guarded(ENTRIES)


def _serve(handler, client=None):
    server = HTTPServer(("127.0.0.1", 0), handler)
    server.client = client
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture()
def stub_gateway():
    server, thread = _serve(_GuardedHandler)
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_guarded_service_harvested_with_correct_key(stub_gateway):
    fetch = http_fetch_tools(stub_gateway, guard_key=KEY)
    tools = fetch("guarded-svc")
    assert tools == HARVESTED


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
    assert [(r.tool, r.description, r.input_schema) for r in recs] == [("secret_tool", "x", SCHEMA)]


def test_harvest_skips_guarded_service_with_wrong_key(stub_gateway):
    from webspec_registry.catalog import harvest

    recs = harvest(["guarded-svc"], http_fetch_tools(stub_gateway, guard_key=WRONG_KEY))
    assert recs == []


# ── Against the real gateway ──


class _Forward(BaseHTTPRequestHandler):
    """Hands each request to the real gateway app (a TestClient over a fake MCP pool), as its socket would."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _forward(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        r = self.server.client.request(self.command, self.path, headers=dict(self.headers.items()), content=body)
        self.send_response(r.status_code)
        self.send_header("Content-Type", r.headers.get("content-type", "application/json"))
        self.send_header("Content-Length", str(len(r.content)))
        self.end_headers()
        self.wfile.write(r.content)

    do_GET = do_OPTIONS = _forward


@pytest.mark.parametrize("level", [0, 1])
def test_harvest_reads_the_real_gateways_discovery(monkeypatch, level):
    from webspec_registry.catalog import harvest

    tools = [tool("read_note", read_only=True, open_world=False),
             tool("send_email", read_only=False, destructive=False, open_world=True)]
    client, pool = make_client(monkeypatch, [entry("svc", level=level)], tools)
    server, thread = _serve(_Forward, client)
    try:
        recs = harvest(["svc"], http_fetch_tools(f"http://127.0.0.1:{server.server_port}",
                                                 guard_key=GATEWAY_KEY if level else None))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert [(r.service, r.tool, r.description, r.input_schema) for r in recs] == [
        ("svc", "read_note", "read_note tool", {"type": "object"}),
        ("svc", "send_email", "send_email tool", {"type": "object"}),
    ]
    assert pool.calls == []
