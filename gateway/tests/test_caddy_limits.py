"""The generated configuration, served by the real Caddy (WEBSPEC_TEST_CADDY, built with caddy-ratelimit).

Caddy gets four listening sockets as fds 3 to 6, the way caddy-webspec.socket passes them
(contract C2): 3 and 5 are the gateway's listener, 4 and 6 the direct one. It runs what
webspec.caddy generates, in front of a stand-in gateway and a stand-in web app, and these hold:

- routing (F8, DP-3, DP-5): services answer on the gateway's listener only, direct sites on the
  direct one only, and each listener's catch-all answers 421;
- the log filter (F25, DP-6): no query, no userinfo (absolute-form and CONNECT targets) and no
  header reach the access logs or the default logger's error entries;
- rate limits (F28): a tunneled client counts by its /64; /__nonce and /__challenge count in zones
  of their own that no spelling escapes; local requests count apart from tunneled ones.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from webspec import caddy
from webspec.caddy import (
    DUAL,
    generate_direct_site_block,
    generate_global_caddyfile,
    generate_site_block,
    write_site_block,
)

CADDY = os.environ.get("WEBSPEC_TEST_CADDY", "")
pytestmark = pytest.mark.skipif(not CADDY or not sys.platform.startswith("linux"),
                                reason="set WEBSPEC_TEST_CADDY to a caddy built with caddy-ratelimit (Linux)")

# Execs caddy with the sockets given as fd numbers moved to 3, 4, 5 and 6, as systemd passes them.
LAUNCHER = """
import fcntl, os, sys
fds = [int(fd) for fd in sys.argv[1:5]]
high = [fcntl.fcntl(fd, fcntl.F_DUPFD, 100) for fd in fds]
for fd in fds:
    os.close(fd)
for target, fd in enumerate(high, start=3):
    os.dup2(fd, target)
    os.close(fd)
os.execv(sys.argv[5], sys.argv[5:])
"""

SECRETS = ("SECRETPW", "SECRETQ", "SECRETTAG", "SECRETAUTH", "SECRETCOOKIE")


class _Echo(http.server.BaseHTTPRequestHandler):
    def _reply(self, body: bool = True) -> None:
        data = f"{self.server.tag} {self.headers.get('Host')}\n".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._reply()

    def do_HEAD(self):  # noqa: N802
        self._reply(False)

    def log_message(self, *args):
        pass


def _upstream(tag: str) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    server.tag = tag
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _listener(family: int, address: str) -> socket.socket:
    s = socket.socket(family, socket.SOCK_STREAM)
    s.bind((address, 0))
    s.listen(128)
    return s


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port: int, host: str, target: str = "/", headers: dict | None = None, method: str = "GET",
            address: str = "127.0.0.1") -> int:
    """Send one request, raw (absolute-form and CONNECT targets included); return its status."""
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host}", *(f"{k}: {v}" for k, v in (headers or {}).items()),
             "Connection: close", "", ""]
    with socket.create_connection((address, port), timeout=10) as s:
        s.sendall("\r\n".join(lines).encode())
        data = b""
        try:
            while b"\r\n" not in data and (chunk := s.recv(65536)):
                data += chunk
        except socket.timeout:  # a CONNECT may be left open; its status line is what counts
            pass
    return int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/") else 0


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """Caddy running the generated configuration on four sockets; the ports of each fd."""
    root = tmp_path_factory.mktemp("caddy")
    gateway, app = _upstream("gateway"), _upstream("app")
    socks = [_listener(socket.AF_INET, "127.0.0.1"), _listener(socket.AF_INET, "127.0.0.1")]
    try:  # [::1] where the kernel has it; otherwise more IPv4 sockets: the fds are what matters
        socks += [_listener(socket.AF_INET6, "::1"), _listener(socket.AF_INET6, "::1")]
        v6 = "::1"
    except OSError:
        socks += [_listener(socket.AF_INET, "127.0.0.1"), _listener(socket.AF_INET, "127.0.0.1")]
        v6 = "127.0.0.1"
    ports = {fd: s.getsockname()[1] for fd, s in enumerate(socks, start=3)}

    conf, logs = root / "conf.d", root / "log"
    logs.mkdir()
    (root / "Caddyfile").write_text(generate_global_caddyfile(conf_dir=conf, admin_socket=root / "admin.sock",
                                                              listen=DUAL, domain="example.com"))
    port = gateway.server_address[1]
    blocks = {
        "svc": generate_site_block("svc", "example.com", gateway_port=port, guard=True, rate_limit=1000,
                                   listen=DUAL.gateway, log_dir=logs),
        "open": generate_site_block("open", "example.com", gateway_port=port, guard=False, rate_limit=1000,
                                    listen=DUAL.gateway, log_dir=logs),
        "lim": generate_site_block("lim", "example.com", gateway_port=port, guard=True, rate_limit=5,
                                   nonce_rate_limit=10, listen=DUAL.gateway, log_dir=logs),
        "down": generate_site_block("down", "example.com", gateway_port=_free_port(), guard=True,
                                    listen=DUAL.gateway, log_dir=logs),
        "web": generate_direct_site_block("web", "example.com", target_port=app.server_address[1],
                                          listen=DUAL.direct, log_dir=logs),
    }
    for name, text in blocks.items():
        write_site_block(name, text, conf_dir=conf)

    stderr = (root / "stderr.log").open("w")
    env = {"PATH": caddy.SAFE_PATH, "HOME": str(root), "XDG_DATA_HOME": str(root / "data"),
           "XDG_CONFIG_HOME": str(root / "config")}
    proc = subprocess.Popen([sys.executable, "-I", "-c", LAUNCHER, *(str(s.fileno()) for s in socks),
                             CADDY, "run", "--config", str(root / "Caddyfile")],
                            pass_fds=[s.fileno() for s in socks], env=env, stdout=stderr, stderr=stderr, cwd=root)
    for s in socks:
        s.close()  # Caddy holds them now, as systemd would
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                if request(ports[3], "unknown.invalid") == 421:
                    break
            except OSError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                stderr.flush()
                pytest.fail(f"caddy did not start:\n{(root / 'stderr.log').read_text()[-4000:]}")
            time.sleep(0.2)
        yield SimpleNamespace(ports=ports, v6=v6, logs=logs, stderr=root / "stderr.log")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        stderr.close()
        gateway.shutdown()
        app.shutdown()


def _on(served, fd: int, host: str, **kwargs) -> int:
    address = served.v6 if fd in (5, 6) else "127.0.0.1"
    return request(served.ports[fd], host, address=address, **kwargs)


# ── F8: each listener serves its own sites (C2) ──


@pytest.mark.parametrize("fd", [3, 5])
def test_the_gateways_listener_serves_services_and_nothing_else(served, fd):
    assert _on(served, fd, "svc.localhost") == 200
    assert _on(served, fd, "svc.example.com") == 200
    assert _on(served, fd, "open.localhost") == 200
    assert _on(served, fd, "open.example.com") == 421  # unguarded: loopback name only
    assert _on(served, fd, "web.localhost") == 421  # a direct site is never on this listener (DP-3)
    assert _on(served, fd, "web.example.com") == 421
    assert _on(served, fd, "unknown.example.com") == 421


@pytest.mark.parametrize("fd", [4, 6])
def test_the_direct_listener_serves_direct_sites_and_nothing_else(served, fd):
    assert _on(served, fd, "web.localhost") == 200
    assert _on(served, fd, "web.example.com") == 200
    assert _on(served, fd, "svc.localhost") == 421
    assert _on(served, fd, "svc.example.com") == 421
    assert _on(served, fd, "unknown.example.com") == 421


@pytest.mark.parametrize("fd,host", [(3, "svc.localhost"), (4, "web.localhost")])
def test_a_loopback_host_never_comes_through_the_tunnel(served, fd, host):
    assert _on(served, fd, host, headers={"Cf-Ray": "8a1b2c3d4e5f-AMS"}) == 421


# ── F25: nothing secret in the logs ──


def test_logs_keep_the_path_only(served):
    secret_headers = {"X-WebSpec-Guard": "SECRETTAG", "Authorization": "Bearer SECRETAUTH",
                      "Cookie": "s=SECRETCOOKIE"}
    targets = [
        (3, "svc.localhost", "GET", "http://user:SECRETPW1@svc.localhost/tool?arg=SECRETQ1", "/tool"),
        (3, "svc.localhost", "GET", "HTTPS://u:SECRETPW2@svc.localhost:7001/a/b?x=SECRETQ2", "/a/b"),
        (3, "svc.localhost", "CONNECT", "u:SECRETPW3@svc.localhost:7001", "svc.localhost:7001"),
        (3, "svc.localhost", "GET", "/plain?token=SECRETQ3", "/plain"),
        (4, "web.localhost", "GET", "http://w:SECRETPW4@web.localhost/page?SECRETQ4", "/page"),
        (3, "down.localhost", "GET", "http://u:SECRETPW5@down.localhost/t?q=SECRETQ5", "/t"),  # a 502
    ]
    for fd, host, method, target, _ in targets:
        _on(served, fd, host, target=target, method=method, headers=secret_headers)
    expected = {"svc": ["/tool", "/a/b", "svc.localhost:7001", "/plain"], "web": ["/page"], "down": ["/t"]}
    deadline = time.monotonic() + 10
    while True:
        logged = {}
        for name in expected:
            path = served.logs / f"{name}.log"
            entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            logged[name] = [e["request"]["uri"] for e in entries if e.get("msg") == "handled request"]
            if any("headers" in e["request"] or "resp_headers" in e for e in entries):
                pytest.fail(f"{name}.log keeps headers")
        if all(logged[name][-len(uris):] == uris for name, uris in expected.items()) or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    for name, uris in expected.items():
        assert logged[name][-len(uris):] == uris, name
    everything = "".join(p.read_text() for p in served.logs.iterdir()) + served.stderr.read_text()
    assert "dial tcp" in served.stderr.read_text()  # the 502's error entry went to the default logger
    for secret in SECRETS:
        assert secret not in everything, secret


# ── F28: the rate-limit zones ──


def _tunneled(served, client: str, path: str = "/x") -> int:
    return _on(served, 3, "lim.example.com", target=path,
               headers={"Cf-Ray": "8a1b2c3d4e5f-AMS", "Cf-Connecting-Ip": client})


def test_a_tunneled_ipv6_client_counts_by_its_64(served):
    assert [_tunneled(served, "2001:db8:1:2::1") for _ in range(5)] == [200] * 5
    assert _tunneled(served, "2001:db8:1:2::1") == 429
    assert _tunneled(served, "2001:db8:1:2:dead:beef:0:9") == 429  # the same /64
    assert _tunneled(served, "2001:db8:1:2:0:0:0:7") == 429  # spelled out
    assert _tunneled(served, "2001:db8:1:3::1") == 200  # the next /64
    assert _tunneled(served, "203.0.113.7") == 200  # IPv4: one address


def test_nonce_paths_count_in_a_zone_of_their_own_that_no_spelling_escapes(served):
    client = "2001:db8:5:5::1"
    assert [_tunneled(served, client, "/__nonce") for _ in range(10)] == [200] * 10
    # Each spelling meets the exhausted nonce zone; in the general zone, still fresh, it would pass.
    for path in ("/__nonce", "/__NONCE", "//__nonce", "/%5F%5Fnonce", "/__challenge", "/x/../__nonce"):
        assert _tunneled(served, client, path) == 429, path
    assert _tunneled(served, client, "/x") == 200


def test_local_requests_count_apart_from_tunneled_ones(served):
    local = [_on(served, 3, "lim.localhost", target="/y") for _ in range(6)]
    assert local[:5] == [200] * 5 and local[5] == 429
    assert _on(served, 3, "lim.localhost", target="/__nonce") == 200  # the local nonce zone
    assert _tunneled(served, "2001:db8:6:6::1") == 200  # the public clients' allowance is untouched
