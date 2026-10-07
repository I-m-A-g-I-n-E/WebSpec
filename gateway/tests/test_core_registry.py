"""The registry dials the gateway by IP, never through a proxy or a redirect, and holds the
guard key only when guarded harvest is turned on (DP-1).

Before, it dialed ``http://localhost:7002``. ``localhost`` resolves to ::1 first on macOS and
Debian while the gateway listens on 127.0.0.1 only, so any local user listening on [::1] at
that port received the registry's requests, and with the guard key in the registry's
environment (as the README advised) its guard tags, for destination names it chose.

Real HTTP servers on loopback stand in for the gateway, a proxy and a redirect target.
Documented exceptions to "no mocks": uvicorn.run (so main() never serves), the resolver (for
names that resolve to loopback), and harden_process (it would make the test process itself
non-dumpable).

Also: an empty WEBSPEC_REGISTRY_HOST means 127.0.0.1, not every interface.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import uvicorn

import webspec.hardening
import webspec_registry.__main__ as registry_main
from webspec.guard import compute_guard_hmac

GATEWAY_DIR = Path(__file__).resolve().parent.parent
HARVEST = "WEBSPEC_REGISTRY_HARVEST_GUARDED"  # the opt-in for guarded harvest
KEY_HEX = "42" * 32
KEY = bytes.fromhex(KEY_HEX)
TOOLS = [{"name": "list_notes", "description": "List notes", "inputSchema": {}}]
SECRET_TOOLS = [{"name": "read_secret", "description": "Read a secret", "inputSchema": {}}]


class _Recorder(HTTPServer):
    def __init__(self, address, handler, family=socket.AF_INET):
        self.address_family = family
        self.seen: list[dict] = []
        self.redirect_to = ""
        self.nonce_redirect_to = ""
        super().__init__(address, handler)


class _Handler(BaseHTTPRequestHandler):
    """A gateway as the registry sees it: index, OPTIONS per destination, the guard flow."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, payload: dict | None = None, headers: dict | None = None) -> None:
        body = json.dumps(payload or {}).encode()
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self) -> tuple[str, str]:
        host = self.headers.get("Host", "")
        self.server.seen.append({"method": self.command, "path": self.path, "host": host,
                                 "guard": self.headers.get("X-WebSpec-Guard"),
                                 "nonce": self.headers.get("X-WebSpec-Nonce")})
        return host, self.path

    def do_GET(self):
        host, path = self._record()
        if (host, path) == ("localhost", "/"):
            self._send(200, {"services": [{"name": n} for n in
                                          ("open-svc", "guarded-svc", "redirect-svc", "Bad Name")]})
        elif (host, path) == ("guarded-svc.localhost", "/__nonce") and self.server.nonce_redirect_to:
            self._send(302, headers={"Location": self.server.nonce_redirect_to})
        elif (host, path) == ("guarded-svc.localhost", "/__nonce"):
            ok = self.headers.get("X-WebSpec-Guard") == compute_guard_hmac(KEY, "GET", host, "/__nonce", "", b"")
            self._send(200, {"nonce": "n-1"}) if ok else self._send(401, {"error": "guard_invalid"})
        else:
            self._send(404, {"error": "not_found"})

    def do_OPTIONS(self):
        host, path = self._record()
        if host == "open-svc.localhost":
            self._send(200, {"tools": TOOLS})
        elif host == "redirect-svc.localhost":
            self._send(302, headers={"Location": self.server.redirect_to})
        elif host == "guarded-svc.localhost":
            nonce = self.headers.get("X-WebSpec-Nonce", "")
            ok = nonce and self.headers.get("X-WebSpec-Guard") == compute_guard_hmac(KEY, "OPTIONS", host, "/",
                                                                                     nonce, b"")
            self._send(200, {"tools": SECRET_TOOLS}) if ok else self._send(401, {"error": "guard_missing"})
        else:
            self._send(404, {"error": "unknown_service"})


def _serve(host: str = "127.0.0.1", family=socket.AF_INET):
    server = _Recorder((host, 0), _Handler, family)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def servers():
    started = []

    def start(host: str = "127.0.0.1", family=socket.AF_INET) -> _Recorder:
        server, thread = _serve(host, family)
        started.append((server, thread))
        return server

    yield start
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _registry_env(monkeypatch):
    for var in ("WEBSPEC_GATEWAY_URL", "WEBSPEC_GUARD_KEY", "WEBSPEC_GUARD_KEY_FILE", "WEBSPEC_GUARD_KEY_DEV_EPHEMERAL",
                HARVEST, "WEBSPEC_REGISTRY_PORT", "WEBSPEC_REGISTRY_HOST", "WEBSPEC_INTERNAL_PORT",
                "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
                "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)


def _url(server: _Recorder) -> str:
    host = server.server_address[0]
    return f"http://{'[' + host + ']' if ':' in host else host}:{server.server_address[1]}"


# ── Where the registry connects ──


def test_the_default_gateway_url_is_the_loopback_ip():
    assert registry_main._gateway_url() == "http://127.0.0.1:7002"


@pytest.mark.parametrize("url", ["http://localhost:7002", "http://LOCALHOST:7002/", "http://localhost.:7002",
                                 "http://notes.localhost:7002", "https://localhost"])
def test_a_loopback_name_is_refused(monkeypatch, url):
    monkeypatch.setenv("WEBSPEC_GATEWAY_URL", url)
    with pytest.raises(ValueError, match="dial the gateway by IP"):
        registry_main._gateway_url()


@pytest.mark.parametrize("url", ["ftp://127.0.0.1/", "file:///etc/passwd", "http://:7002", "127.0.0.1:7002",
                                 "http://127.0.0.1:port"])
def test_a_url_that_is_not_http_is_refused(monkeypatch, url):
    monkeypatch.setenv("WEBSPEC_GATEWAY_URL", url)
    with pytest.raises(ValueError, match="is not an http:// URL"):
        registry_main._gateway_url()


@pytest.mark.parametrize("url", ["http://127.0.0.1:7002", "http://[::1]:7002", "http://10.1.2.3:7002",
                                 "https://192.0.2.10"])
def test_an_ip_literal_is_accepted(monkeypatch, url):
    monkeypatch.setenv("WEBSPEC_GATEWAY_URL", url)
    assert registry_main._gateway_url() == url


@pytest.mark.parametrize("addresses, refused", [
    (["127.0.0.2"], True), (["::1", "10.0.0.7"], True), (["10.0.0.7"], False), (OSError("no such name"), False),
])
def test_any_other_name_is_refused_when_it_resolves_to_loopback(monkeypatch, addresses, refused):
    def fake_getaddrinfo(host, port, *args, **kwargs):  # documented exception: the resolver
        assert host == "gateway"
        if isinstance(addresses, Exception):
            raise addresses
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    monkeypatch.setattr(registry_main.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setenv("WEBSPEC_GATEWAY_URL", "http://gateway:7002")
    if refused:
        with pytest.raises(ValueError, match="names a loopback address"):
            registry_main._gateway_url()
    else:
        assert registry_main._gateway_url() == "http://gateway:7002"


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _ipv6_loopback(), reason="no IPv6 loopback")
@pytest.mark.parametrize("guarded", [False, True])
def test_a_listener_on_ipv6_loopback_receives_nothing(monkeypatch, servers, guarded):
    """The finding's reproduction: a local user listens on [::1] at the gateway's port."""
    squatter = servers("::1", socket.AF_INET6)
    port = squatter.server_address[1]
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", KEY_HEX)
    if guarded:
        monkeypatch.setenv(HARVEST, "1")
    for url in (f"http://localhost:{port}", f"http://127.0.0.1:{port}"):
        monkeypatch.setenv("WEBSPEC_GATEWAY_URL", url)
        assert registry_main._default_catalog() == []
    assert squatter.seen == []


# ── What it sends ──


def test_it_sends_loopback_host_names_and_no_guard_tags_by_default(monkeypatch, servers):
    gateway, sink = servers(), servers()
    gateway.redirect_to = _url(sink) + "/"
    monkeypatch.setenv("WEBSPEC_GATEWAY_URL", _url(gateway))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", KEY_HEX)  # present, but guarded harvest is off
    records = registry_main._default_catalog()
    assert [(r.service, r.tool) for r in records] == [("open-svc", "list_notes")]
    assert [(s["method"], s["path"], s["host"]) for s in gateway.seen] == [
        ("GET", "/", "localhost"),
        ("OPTIONS", "/", "open-svc.localhost"),
        ("OPTIONS", "/", "guarded-svc.localhost"),  # 401: skipped, no guard flow
        ("OPTIONS", "/", "redirect-svc.localhost"),  # 302: not followed
    ]
    assert not any(s["guard"] or s["nonce"] for s in gateway.seen)
    assert sink.seen == []


def test_guarded_harvest_is_opt_in(monkeypatch, servers):
    gateway = servers()
    monkeypatch.setenv("WEBSPEC_GATEWAY_URL", _url(gateway))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", KEY_HEX)
    monkeypatch.setenv(HARVEST, "1")
    records = registry_main._default_catalog()
    assert sorted((r.service, r.tool) for r in records) == [("guarded-svc", "read_secret"), ("open-svc", "list_notes")]
    assert [(s["method"], s["path"]) for s in gateway.seen if s["host"] == "guarded-svc.localhost"] == [
        ("OPTIONS", "/"), ("GET", "/__nonce"), ("OPTIONS", "/")]


def test_guarded_harvest_without_a_key_says_so(monkeypatch, servers, caplog):
    gateway = servers()
    monkeypatch.setenv("WEBSPEC_GATEWAY_URL", _url(gateway))
    monkeypatch.setenv(HARVEST, "1")
    with caplog.at_level(logging.WARNING, logger="webspec.registry"):
        records = registry_main._default_catalog()
    assert [r.service for r in records] == ["open-svc"]
    assert "the guard key is unavailable" in caplog.text and "WEBSPEC_GUARD_KEY is not set" in caplog.text


def test_proxies_from_the_environment_are_not_used(monkeypatch, servers):
    gateway, proxy = servers(), servers()
    monkeypatch.setenv("WEBSPEC_GATEWAY_URL", _url(gateway))
    monkeypatch.setenv("http_proxy", _url(proxy))
    assert [r.tool for r in registry_main._default_catalog()] == ["list_notes"]
    assert proxy.seen == [] and gateway.seen


def test_a_proxy_set_before_the_registry_starts_is_not_used(servers):
    """urllib reads the proxy variables when the opener is built, at import: so set them first,
    as a login shell would, in a process of its own."""
    gateway, proxy = servers(), servers()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "WEBSPEC_GATEWAY_URL": _url(gateway), "WEBSPEC_GUARD_KEY": KEY_HEX, HARVEST: "1",
           **{name: _url(proxy) for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY")}}
    code = ("import sys; import webspec_registry.__main__ as m; "
            "print(sorted((r.service, r.tool) for r in m._default_catalog()))")
    out = subprocess.run([sys.executable, "-c", code], cwd=GATEWAY_DIR, env=env, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert proxy.seen == []
    assert out.stdout.strip().splitlines()[-1] == str([("guarded-svc", "read_secret"), ("open-svc", "list_notes")])


def test_a_redirect_does_not_carry_the_guard_tag_elsewhere(monkeypatch, servers):
    """urllib follows a 302 on GET and sends the Host and X-WebSpec-Guard headers along."""
    gateway, sink = servers(), servers()
    gateway.nonce_redirect_to = _url(sink) + "/__nonce"
    monkeypatch.setenv("WEBSPEC_GATEWAY_URL", _url(gateway))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", KEY_HEX)
    monkeypatch.setenv(HARVEST, "1")
    assert [r.service for r in registry_main._default_catalog()] == ["open-svc"]  # guarded-svc: 302, skipped
    [nonce_request] = [s for s in gateway.seen if s["path"] == "/__nonce"]
    assert nonce_request["host"] == "guarded-svc.localhost" and nonce_request["guard"]  # signed, to the gateway
    assert sink.seen == []


# ── At startup ──


class _Served(Exception):
    pass


def _no_serving(*args, **kwargs):  # documented exception: main() must not start a server here
    raise _Served(kwargs)


@pytest.mark.parametrize("url", ["http://localhost:7002", "http://notes.localhost:7002"])
def test_main_refuses_a_loopback_name_before_serving(monkeypatch, url):
    monkeypatch.setattr(registry_main.uvicorn, "run", _no_serving)
    monkeypatch.setenv("WEBSPEC_GATEWAY_URL", url)
    with pytest.raises(SystemExit, match="dial the gateway by IP"):
        registry_main.main()


def test_main_warns_about_a_guard_key_it_does_not_use(monkeypatch, caplog):
    monkeypatch.setattr(registry_main.uvicorn, "run", _no_serving)
    hardened = []
    monkeypatch.setattr(webspec.hardening, "harden_process", lambda: hardened.append(True))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", KEY_HEX)
    with caplog.at_level(logging.WARNING, logger="webspec.registry"), pytest.raises(_Served):
        registry_main.main()
    assert "WEBSPEC_GUARD_KEY is set but not used" in caplog.text
    assert KEY_HEX not in caplog.text and hardened == []


def test_main_hardens_the_process_when_it_holds_the_key(monkeypatch, caplog):
    monkeypatch.setattr(registry_main.uvicorn, "run", _no_serving)
    hardened = []
    monkeypatch.setattr(webspec.hardening, "harden_process", lambda: hardened.append(True))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", KEY_HEX)
    monkeypatch.setenv(HARVEST, "1")
    with caplog.at_level(logging.WARNING, logger="webspec.registry"), pytest.raises(_Served):
        registry_main.main()
    assert hardened == [True]
    assert "run it as the gateway's own user, never as the agent's (DP-1)" in caplog.text
    # It also says who can then read the guarded destinations' tool lists, and where.
    assert "without the guard to every process that can reach 127.0.0.1:7004, the agent's included" in caplog.text
    assert KEY_HEX not in caplog.text

    caplog.clear()
    monkeypatch.setenv("WEBSPEC_REGISTRY_HOST", "::1")
    with caplog.at_level(logging.WARNING, logger="webspec.registry"), pytest.raises(_Served):
        registry_main.main()
    assert "every process that can reach [::1]:7004" in caplog.text


# ── Where the registry listens ──


def test_the_registry_listens_on_its_own_port(monkeypatch, caplog):
    """Not on 7003, which Caddy holds for direct sites (fed public traffic by cloudflared), and
    not on WEBSPEC_INTERNAL_PORT, which is the gateway's."""
    monkeypatch.setattr(registry_main.uvicorn, "run", _no_serving)
    with pytest.raises(_Served) as served:
        registry_main.main()
    assert served.value.args[0] == {"host": "127.0.0.1", "port": 7004}

    monkeypatch.setenv("WEBSPEC_INTERNAL_PORT", "7002")  # the gateway's environment, sourced by mistake
    with caplog.at_level(logging.WARNING, logger="webspec.registry"), pytest.raises(_Served) as served:
        registry_main.main()
    assert served.value.args[0]["port"] == 7004
    assert "WEBSPEC_INTERNAL_PORT is the gateway's listen port and is ignored" in caplog.text

    monkeypatch.setenv("WEBSPEC_REGISTRY_PORT", "7010")
    with pytest.raises(_Served) as served:
        registry_main.main()
    assert served.value.args[0]["port"] == 7010


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_an_empty_registry_host_means_loopback(monkeypatch, value):
    """uvicorn binds "" on every interface; compose's ${VAR} gives "" for an unset variable."""
    monkeypatch.setattr(registry_main.uvicorn, "run", _no_serving)
    monkeypatch.setenv("WEBSPEC_REGISTRY_HOST", value)
    with pytest.raises(_Served) as served:
        registry_main.main()
    assert served.value.args[0]["host"] == "127.0.0.1"
    sock = uvicorn.Config(app=None, host=registry_main._resolve_registry_host(), port=0).bind_socket()
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_a_registry_host_that_is_set_is_kept(monkeypatch):
    monkeypatch.setenv("WEBSPEC_REGISTRY_HOST", " ::1 ")
    assert registry_main._resolve_registry_host() == "::1"


@pytest.mark.parametrize("value", ["x", "0", "70000", "-1", "7O04", "٧٠٠٤"])
def test_a_malformed_registry_port_stops_main(monkeypatch, value):
    monkeypatch.setattr(registry_main.uvicorn, "run", _no_serving)
    monkeypatch.setenv("WEBSPEC_REGISTRY_PORT", value)
    with pytest.raises(SystemExit, match="WEBSPEC_REGISTRY_PORT .* is not a port number"):
        registry_main.main()
