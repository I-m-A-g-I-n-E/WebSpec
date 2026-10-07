"""The gateway's listening socket under systemd and launchd (webspec/activation.py; DP-8, DP-9).

The service manager binds 127.0.0.1:7002 and keeps it bound while the gateway restarts, so no
local process can take the port between two gateway processes and receive what Caddy forwards.
The systemd path runs for real here: a listening socket at descriptor 3 with LISTEN_PID and
LISTEN_FDS set, in a subprocess, the way systemd starts a socket-activated service. The
launchd path runs against a stand-in for launch_activate_socket(3), built with ctypes like the
real one; on macOS the real function is called too, and refuses this process (no launchd job).
"""

import ctypes
import errno
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import webspec.__main__ as gateway_main
from webspec import activation, pool

GATEWAY = Path(__file__).resolve().parents[1]
GUARD_KEY_HEX = "11" * 32

# Starts the rest of argv the way systemd starts a socket-activated service: the socket (the
# descriptor named by argv[1]) becomes descriptor 3, and LISTEN_PID names the process that
# receives it, which keeps this PID through exec. argv[2] is the LISTEN_PID to set ("self" for
# this process), argv[3] the LISTEN_FDS and argv[4] the LISTEN_FDNAMES, which names the holder:
# systemd's default is the socket unit's name, and the Docker image's init uses webspec-init.
LAUNCHER = """
import os, sys
fd, listen_pid, listen_fds, fdnames = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
os.dup2(fd, 3)
if fd != 3:
    os.close(fd)
os.environ.update(LISTEN_PID=str(os.getpid()) if listen_pid == "self" else listen_pid,
                  LISTEN_FDS=listen_fds, LISTEN_FDNAMES=fdnames)
os.execv(sys.executable, [sys.executable] + sys.argv[5:])
"""

# Takes the socket as the gateway does and reports what it got and what is left behind.
PROBE = """
import json, os
from webspec import activation
got = activation.inherited_socket()
print(json.dumps({"source": got.source, "address": got.address, "fd": got.socket.fileno(),
                  "inheritable": os.get_inheritable(got.socket.fileno()),
                  "left": sorted(k for k in os.environ if k.startswith("LISTEN_"))}))
"""


def child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LISTEN_", "WEBSPEC_", "PYTHON"))}
    return {**env, **extra}


SYSTEMD_FDNAMES = "webspec-gateway.socket"  # what systemd sets for webspec-gateway.socket
INIT_FDNAMES = "webspec-init"  # what docker/init.py --listen sets


def activated(fd: int, argv: list[str], *, listen_pid: str = "self", listen_fds: str = "1",
              fdnames: str = SYSTEMD_FDNAMES, env: dict[str, str] | None = None) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", LAUNCHER, str(fd), listen_pid, listen_fds, fdnames, *argv],
                            pass_fds=(fd,), cwd=GATEWAY, env=env or child_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def listening_socket(family: int = socket.AF_INET, host: str = "127.0.0.1") -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.bind((host, 0))
    sock.listen(16)
    return sock


@pytest.fixture
def gateway_env(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mcpServers": {}}))

    def make(port: int) -> dict[str, str]:
        return child_env(WEBSPEC_CONFIG=str(config), WEBSPEC_GUARD_KEY=GUARD_KEY_HEX, WEBSPEC_AUDIT_LOG="",
                         WEBSPEC_HOST="127.0.0.1", WEBSPEC_PORT="7001", WEBSPEC_INTERNAL_PORT=str(port))
    return make


# ── systemd, for real ────────────────────────────────────────────────────────


def test_takes_the_systemd_socket_and_removes_the_variables():
    with listening_socket() as sock:
        proc = activated(sock.fileno(), ["-c", PROBE])
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        got = json.loads(out)
        assert got == {"source": "systemd", "address": "127.0.0.1:%d" % sock.getsockname()[1], "fd": 3,
                       "inheritable": False,  # never handed to a stdio server or ssh-keygen
                       "left": []}  # no child sees LISTEN_*


def test_takes_an_ipv6_loopback_socket():
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    try:
        sock = listening_socket(socket.AF_INET6, "::1")
    except OSError:
        pytest.skip("no IPv6 loopback")
    with sock:
        proc = activated(sock.fileno(), ["-c", PROBE])
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        assert json.loads(out)["address"] == "[::1]:%d" % sock.getsockname()[1]


@pytest.mark.parametrize("fdnames, held", [
    (SYSTEMD_FDNAMES, "socket held by systemd: it stays bound while the gateway restarts)"),
    # The Docker image's init never restarts the gateway: it exits with it, and the container too.
    (INIT_FDNAMES, "socket held by webspec-init: it stays bound while the gateway shuts down)"),
])
def test_the_gateway_serves_on_the_socket_and_the_port_outlives_it(gateway_env, fdnames, held):
    """DP-9: the gateway accepts on the socket it inherits and binds nothing itself
    (WEBSPEC_INTERNAL_PORT names the same port, so a bind of its own would fail). When it
    exits, the port is still held by whoever passed the socket, as systemd holds it, and
    nobody else can bind it. The log names the holder, and says what it keeps the port for."""
    with listening_socket() as sock:
        port = sock.getsockname()[1]
        proc = activated(sock.fileno(), ["-m", "webspec"], fdnames=fdnames, env=gateway_env(port))
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            conn.request("GET", "/", headers={"Host": f"localhost:{port}"})
            response = conn.getresponse()
            assert response.status == 200
            assert "services" in json.loads(response.read())
            conn.close()
        finally:
            proc.send_signal(signal.SIGTERM)
            _, err = proc.communicate(timeout=30)
        assert f"Listening on http://127.0.0.1:{port} ({held}" in err
        assert "Uvicorn running on" not in err  # uvicorn bound nothing
        assert "address already in use" not in err.lower()

        squatter = socket.socket()
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with squatter, pytest.raises(OSError) as exc:
            squatter.bind(("127.0.0.1", port))
        assert exc.value.errno == errno.EADDRINUSE
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass  # still listening: a connection now waits for the next gateway


def gateway_refuses(proc: subprocess.Popen) -> str:
    try:
        _, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()  # it started after all: never leave it running
        raise AssertionError(f"the gateway did not exit: {proc.communicate()[1]}") from None
    assert proc.returncode == gateway_main.STARTUP_FAILURE, err
    lines = [ln for ln in err.splitlines() if "Socket activation failed" in ln]
    assert len(lines) == 1, err  # one clear line
    assert "Uvicorn running on" not in err and "Application startup" not in err
    return lines[0]


@pytest.mark.parametrize("fdnames, holder", [(SYSTEMD_FDNAMES, "systemd"), (INIT_FDNAMES, "webspec-init")])
def test_the_gateway_exits_rather_than_bind_when_the_socket_is_not_tcp(gateway_env, fdnames, holder):
    # The error names whoever passed the socket: under Docker that is the image's init, and an
    # operator sent to systemd would look for a service manager the container does not have.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as spare:
        udp.bind(("127.0.0.1", 0))
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
        spare.close()  # free: the gateway could bind it, and must not
        line = gateway_refuses(activated(udp.fileno(), ["-m", "webspec"], fdnames=fdnames, env=gateway_env(port)))
        assert f"the socket from {holder} (fd 3) is not a TCP stream socket" in line


def test_the_gateway_exits_when_descriptor_3_is_not_a_socket(gateway_env, tmp_path):
    with open(tmp_path / "file", "w") as f:
        line = gateway_refuses(activated(f.fileno(), ["-m", "webspec"], env=gateway_env(7002)))
    assert "is not a socket" in line


@pytest.mark.parametrize("listen_pid, listen_fds, message", [
    ("self", "2", "expected exactly one"),
    ("self", "0", "expected exactly one"),
    ("x", "1", "LISTEN_PID is not a process ID"),
])
def test_the_gateway_exits_on_variables_that_do_not_add_up(gateway_env, listen_pid, listen_fds, message):
    with listening_socket() as sock:
        proc = activated(sock.fileno(), ["-m", "webspec"], listen_pid=listen_pid, listen_fds=listen_fds,
                         env=gateway_env(sock.getsockname()[1]))
        assert message in gateway_refuses(proc)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serves(port: int, proc: subprocess.Popen) -> int:
    """The status of GET / on 127.0.0.1:port once the gateway listens (or its exit, if it dies)."""
    for _ in range(300):
        if proc.poll() is not None:
            raise AssertionError(f"the gateway exited with {proc.returncode}: {proc.communicate()[1]}")
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/", headers={"Host": f"localhost:{port}"})
            status = conn.getresponse().status
            conn.close()
            return status
        except OSError:
            time.sleep(0.1)
    raise AssertionError("the gateway never listened")


def stop(proc: subprocess.Popen) -> str:
    proc.send_signal(signal.SIGTERM)
    return proc.communicate(timeout=60)[1]


def test_variables_meant_for_another_process_are_ignored(gateway_env):
    """sd_listen_fds(3): LISTEN_PID that names another process means some parent was socket
    activated and left them behind. The gateway ignores them (and removes them, so no child sees
    them) and binds WEBSPEC_HOST:WEBSPEC_INTERNAL_PORT itself, as without them. (Under the unit
    the gateway is the main process, so LISTEN_PID is its own.)"""
    with listening_socket() as sock:
        # Asked for while sock holds its own port, so the kernel cannot hand out that one again.
        port = free_port()
        proc = activated(sock.fileno(), ["-m", "webspec"], listen_pid="1", env=gateway_env(port))
        try:
            assert serves(port, proc) == 200
        finally:
            err = stop(proc)
    assert "Ignoring LISTEN_PID=1: systemd passed those sockets to that process" in err
    assert f"Uvicorn running on http://127.0.0.1:{port}" in err and "Listening on" not in err


@pytest.mark.parametrize("env, ignored", [
    ({"LISTEN_PID": "4242", "LISTEN_FDS": "1"}, "Ignoring LISTEN_PID=4242"),
    ({"LISTEN_FDS": "1", "LISTEN_FDNAMES": "x"}, "without LISTEN_PID"),
])
def test_systemd_variables_of_another_process_are_ignored(caplog, env, ignored):
    env = {**env, "LISTEN_PIDFDID": "7"}
    with caplog.at_level("WARNING", logger="webspec.activation"):
        assert activation.inherited_socket(env, pid=100) is None
    assert ignored in caplog.text
    assert not [k for k in env if k.startswith("LISTEN_")]  # removed: no child sees them


# ── No activation: the gateway binds the port itself ─────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", ""])
def test_without_activation_the_gateway_binds_its_own_port(gateway_env, host):
    """python -m webspec in a shell, the development units, Docker: no LISTEN_* and no
    WEBSPEC_LAUNCHD_SOCKET, so it binds WEBSPEC_HOST at WEBSPEC_INTERNAL_PORT. An empty
    WEBSPEC_HOST counts as unset: loopback, never every interface (F20, DP-8)."""
    port = free_port()
    env = {**gateway_env(port), "WEBSPEC_HOST": host}
    proc = subprocess.Popen([sys.executable, "-m", "webspec"], cwd=GATEWAY, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert serves(port, proc) == 200
    finally:
        err = stop(proc)
    assert f"Uvicorn running on http://127.0.0.1:{port}" in err
    assert "Listening on" not in err and "Socket activation failed" not in err


# ── Shutdown ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("activated_socket", [False, True])
def test_main_bounds_the_graceful_shutdown(monkeypatch, activated_socket):
    """F3: both paths hand uvicorn a graceful-shutdown limit, so a request that never ends cannot
    keep the gateway in shutdown, and the service manager restarts it."""
    seen = {}

    class Server:
        started = True

        def __init__(self, config):
            seen.update(config=config)

        def run(self, sockets=None):
            seen.update(sockets=sockets)

    monkeypatch.setattr(gateway_main, "harden_process", lambda: None)
    monkeypatch.setattr(gateway_main, "create_app", lambda: object())
    monkeypatch.setattr(gateway_main.uvicorn, "run", lambda app, **kw: seen.update(run=kw))
    monkeypatch.setattr(gateway_main.uvicorn, "Server", Server)
    monkeypatch.setattr(gateway_main.uvicorn, "Config", lambda app, **kw: kw)
    monkeypatch.setenv("WEBSPEC_INTERNAL_PORT", "7002")
    with listening_socket() as sock:
        inherited = activation.Inherited(sock, "systemd") if activated_socket else None
        monkeypatch.setattr(gateway_main.activation, "inherited_socket", lambda: inherited)
        gateway_main.main()
    options = seen["config"] if activated_socket else seen["run"]
    assert options["timeout_graceful_shutdown"] == gateway_main.GRACEFUL_SHUTDOWN_SECONDS
    assert gateway_main.GRACEFUL_SHUTDOWN_SECONDS > pool.DEFAULT_TIMEOUT  # a running tool call may finish


# A stdio server that sends its parent, the gateway, SIGTERM and never answers the MCP
# handshake, while it keeps its pipe open: the request that started it never ends.
HOLDER = "import os, signal, sys; os.kill(os.getppid(), signal.SIGTERM); sys.stdin.read()"
SHORT_SHUTDOWN = "import webspec.__main__ as m; m.GRACEFUL_SHUTDOWN_SECONDS = 2; m.main()"


def test_a_shutdown_ends_although_a_request_never_does(tmp_path, gateway_env):
    """F3: without a limit, uvicorn waits for that request for good: the process lives on,
    answering nothing, so Restart=always never fires. With one, it cancels the request and
    exits, and the service manager starts it again."""
    config = tmp_path / "holder.json"
    config.write_text(json.dumps({"mcpServers": {"holder": {"command": sys.executable, "args": ["-c", HOLDER]}}}))
    port = free_port()
    env = {**gateway_env(port), "WEBSPEC_CONFIG": str(config)}
    proc = subprocess.Popen([sys.executable, "-c", SHORT_SHUTDOWN], cwd=GATEWAY, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert serves(port, proc) == 200
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        conn.request("GET", "/", headers={"Host": f"holder.localhost:{port}"})
        with pytest.raises((http.client.HTTPException, OSError)):
            conn.getresponse()  # the server goes away under it
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("the gateway never finished shutting down")
    finally:
        if proc.poll() is None:
            proc.kill()
        err = proc.communicate()[1]
    assert "timeout graceful shutdown exceeded" in err, err


# A longer grace than the gateway's, so that a slow machine cannot make the test late.
LONG_GRACE = "import webspec.__main__ as m; m.ACCEPTED_GRACE_SECONDS = 2; m.main()"


def test_a_stop_answers_a_request_on_a_connection_it_had_accepted(gateway_env):
    """uvicorn's stop closed every connection it was not reading a request on, so a request that
    arrived on one just then, from a client that had just connected or on a connection Caddy kept
    alive, was reset: 2 of 38,319 new connections across 8 restarts under load, and one request
    per kept-alive client per restart. The gateway stops accepting first, then answers what
    arrives on the connections it has open for ACCEPTED_GRACE_SECONDS, each time with
    Connection: close."""
    with listening_socket() as sock:
        port = sock.getsockname()[1]
        proc = activated(sock.fileno(), ["-c", LONG_GRACE], env=gateway_env(port))
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            conn.request("GET", "/", headers={"Host": f"localhost:{port}"})
            assert conn.getresponse().read()  # kept alive, and idle now
            proc.send_signal(signal.SIGTERM)
            time.sleep(0.5)  # the stop has begun: uvicorn alone has closed the connection by now
            conn.request("GET", "/", headers={"Host": f"localhost:{port}"})
            response = conn.getresponse()
            assert response.status == 200
            assert "services" in json.loads(response.read())
            # A client that keeps its connection alive is told to open the next one, which waits
            # for the next gateway, rather than send it on this one as the gateway closes it.
            assert response.getheader("Connection") == "close"
            conn.close()
            # Then it ends as any stop does: uvicorn re-raises the signal once it has shut down.
            assert proc.wait(timeout=60) == -signal.SIGTERM
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="macOS cannot tell a listening socket")
def test_refuses_a_socket_that_is_not_listening():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        proc = activated(sock.fileno(), ["-c", PROBE])
        _, err = proc.communicate(timeout=60)
        assert proc.returncode != 0 and "is not listening" in err


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="an all-interfaces listener can prompt the macOS firewall")
def test_refuses_a_socket_that_is_not_on_loopback():
    with listening_socket(host="0.0.0.0") as sock:
        proc = activated(sock.fileno(), ["-c", PROBE])
        _, err = proc.communicate(timeout=60)
        assert proc.returncode != 0 and "not to a loopback address" in err


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="an all-interfaces listener can prompt the macOS firewall")
def test_a_socket_on_the_address_webspec_host_names_is_accepted():
    """DP-8's explicit override, as the Docker image uses it: its init holds 0.0.0.0:7002 in
    the container's own network namespace, and docker/docker-compose.yml sets WEBSPEC_HOST=0.0.0.0."""
    with listening_socket(host="0.0.0.0") as sock:
        proc = activated(sock.fileno(), ["-c", PROBE], env=child_env(WEBSPEC_HOST="0.0.0.0"))
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        assert json.loads(out)["address"] == "0.0.0.0:%d" % sock.getsockname()[1]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="an all-interfaces listener can prompt the macOS firewall")
@pytest.mark.parametrize("host", ["127.0.0.1", "::", "localhost", " "])
def test_a_socket_off_loopback_needs_webspec_host_to_name_exactly_its_address(host):
    with listening_socket(host="0.0.0.0") as sock:
        proc = activated(sock.fileno(), ["-c", PROBE], env=child_env(WEBSPEC_HOST=host))
        _, err = proc.communicate(timeout=60)
        assert proc.returncode != 0 and "not to a loopback address" in err


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="an all-interfaces listener can prompt the macOS firewall")
def test_a_host_name_never_names_the_address_of_a_socket_from_the_init(gateway_env):
    """Only an IP address in WEBSPEC_HOST names the address of a socket off loopback: a name
    never matches, whatever it resolves to. The gateway exits without serving, and says that
    the Docker image's init passed the socket, not systemd."""
    with listening_socket(host="0.0.0.0") as sock:
        port = sock.getsockname()[1]
        env = {**gateway_env(port), "WEBSPEC_HOST": "gateway"}
        line = gateway_refuses(activated(sock.fileno(), ["-m", "webspec"], fdnames=INIT_FDNAMES, env=env))
    assert f"the socket from webspec-init (fd 3) is bound to 0.0.0.0:{port}, not to a loopback address" in line
    assert line.endswith("(DP-8); WEBSPEC_HOST='gateway' is not an IP address, and only an IP address names the "
                         "address of a socket passed in")
    assert "systemd" not in line


@pytest.mark.parametrize("fdnames, holder", [(SYSTEMD_FDNAMES, "systemd"), (INIT_FDNAMES, "webspec-init")])
@pytest.mark.parametrize("host", ["gateway", "localhost", "0.0.0.0", "10.0.0.5", "::"])
def test_a_socket_on_loopback_is_taken_whatever_webspec_host_says(fdnames, holder, host):
    """DP-8: WEBSPEC_HOST matters only for a socket off loopback. One on loopback is taken
    whatever it names, a host name included, so the rule for WEBSPEC_HOST is not that it
    must be an IP address whenever a socket is passed in."""
    with listening_socket() as sock:
        proc = activated(sock.fileno(), ["-c", PROBE], fdnames=fdnames, env=child_env(WEBSPEC_HOST=host))
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        got = json.loads(out)
        assert (got["source"], got["address"]) == (holder, "127.0.0.1:%d" % sock.getsockname()[1])


@pytest.mark.parametrize("override, address", [
    ("0.0.0.0", True), ("[::]", True), ("::1", True), ("10.0.0.5", True),
    ("gateway", False), ("localhost", False), ("0", False),  # "0" is 0.0.0.0 to getaddrinfo, not to ipaddress
])
def test_only_an_ip_address_can_name_a_socket_address(override, address):
    assert activation._is_address(override) is address


@pytest.mark.parametrize("override, host, named", [
    ("0.0.0.0", "0.0.0.0", True),
    ("[::]", "::", True),
    ("::", "::", True),
    ("", "0.0.0.0", False),
    ("::", "0.0.0.0", False),  # IPv6 all-interfaces is not IPv4's
    ("10.0.0.5", "10.0.0.6", False),
    ("localhost", "127.0.0.1", False),  # a name is not an address
])
def test_webspec_host_names_an_address_exactly(override, host, named):
    assert activation._named(override, host) is named


@pytest.mark.parametrize("fdnames, holder", [
    (None, "systemd"),
    ("", "systemd"),
    ("webspec-gateway.socket", "systemd"),  # systemd's default: the socket unit's name
    ("webspec-gateway.socket:webspec-gateway.socket", "systemd"),  # one name per descriptor
    ("webspec-init", "webspec-init"),  # the Docker image's init (docker/init.py --listen)
    ("webspec-init:webspec-init", "webspec-init"),
    ("a:b", "a:b"),
])
def test_the_holder_is_named_after_the_parent_that_passed_the_socket(fdnames, holder):
    assert activation._holder(fdnames) == holder


@pytest.mark.parametrize("env, message", [
    ({"LISTEN_PID": "100", "LISTEN_FDS": "2", "LISTEN_FDNAMES": "webspec-init:webspec-init"},
     "webspec-init passed 2 sockets (LISTEN_FDS); expected exactly one"),
    ({"LISTEN_PID": "100", "LISTEN_FDNAMES": "webspec-init"}, "webspec-init passed no sockets (LISTEN_FDS)"),
])
def test_errors_about_the_variables_name_the_holder(env, message):
    with pytest.raises(activation.ActivationError) as exc:
        activation.inherited_socket(env, pid=100)
    assert str(exc.value).startswith(message)
    assert "webspec-gateway.socket" not in str(exc.value)  # systemd's unit is no hint here


def test_variables_of_another_process_name_the_holder_too(caplog):
    env = {"LISTEN_PID": "4242", "LISTEN_FDS": "1", "LISTEN_FDNAMES": "webspec-init"}
    with caplog.at_level("WARNING", logger="webspec.activation"):
        assert activation.inherited_socket(env, pid=100) is None
    assert "Ignoring LISTEN_PID=4242: webspec-init passed those sockets to that process" in caplog.text


@pytest.mark.parametrize("holder, kept", [
    ("systemd", "restarts"),
    ("launchd", "restarts"),
    ("webspec-init", "shuts down"),  # the Docker image's init: it exits with the gateway
    ("some-parent", "shuts down"),
])
def test_the_startup_line_says_how_long_the_holder_keeps_the_port(monkeypatch, caplog, holder, kept):
    class Server:
        started = True

        def __init__(self, config):
            pass

        def run(self, sockets=None):
            pass

    monkeypatch.setattr(gateway_main, "harden_process", lambda: None)
    monkeypatch.setattr(gateway_main, "create_app", lambda: object())
    monkeypatch.setattr(gateway_main.uvicorn, "Server", Server)
    monkeypatch.setattr(gateway_main.uvicorn, "Config", lambda app, **kw: kw)
    with listening_socket() as sock:
        port = sock.getsockname()[1]
        monkeypatch.setenv("WEBSPEC_INTERNAL_PORT", str(port))
        monkeypatch.setattr(gateway_main.activation, "inherited_socket", lambda: activation.Inherited(sock, holder))
        with caplog.at_level("INFO", logger="webspec"):
            gateway_main.main()
    assert (f"Listening on http://127.0.0.1:{port} (socket held by {holder}: it stays bound while the gateway "
            f"{kept})") in caplog.messages


def test_the_gateway_exits_when_the_launchd_socket_cannot_be_had(gateway_env):
    """WEBSPEC_LAUNCHD_SOCKET asks for launchd's socket: outside a launchd job (or off macOS)
    there is none, and the gateway exits instead of binding the port itself."""
    env = {**gateway_env(7002), "WEBSPEC_LAUNCHD_SOCKET": "gateway"}
    proc = subprocess.Popen([sys.executable, "-m", "webspec"], cwd=GATEWAY, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = gateway_refuses(proc)
    assert ("launch_activate_socket('gateway') failed" in line if sys.platform == "darwin"
            else "launchd sockets exist on macOS only" in line)


# ── systemd, the variables alone ────────────────────────────────────────────


def test_no_activation_without_the_variables():
    assert activation.inherited_socket({"PATH": "/usr/bin"}) is None


@pytest.mark.parametrize("env, message", [
    ({"LISTEN_PID": "x", "LISTEN_FDS": "1"}, "LISTEN_PID is not a process ID ('x')"),
    ({"LISTEN_PID": "0", "LISTEN_FDS": "1"}, "LISTEN_PID is not a process ID ('0')"),
    ({"LISTEN_PID": "100"}, "expected exactly one"),  # this process's, with no socket
    ({"LISTEN_PID": "100", "LISTEN_FDS": "3", "LISTEN_FDNAMES": "a:b:c"}, "passed 3 sockets"),
])
def test_systemd_variables_that_do_not_add_up_are_refused(env, message):
    env = {**env, "LISTEN_PIDFDID": "7"}
    with pytest.raises(activation.ActivationError) as exc:
        activation.inherited_socket(env, pid=100)
    assert message in str(exc.value)
    assert not [k for k in env if k.startswith("LISTEN_")]  # removed either way


# ── launchd ───────────────────────────────────────────────────────────────────


class FakeLaunchd:
    """launch_activate_socket(3) and free(3), as ctypes functions with the real signatures.

    The array of descriptors lives in Python memory; free() records the address it is given.
    """

    def __init__(self, fds=(), err=0):
        self.array = (ctypes.c_int * max(1, len(fds)))(*fds)
        self.count = len(fds)
        self.err = err
        self.names: list[bytes] = []
        self.freed: list[int | None] = []
        self.activate = activation.LaunchActivateSocket(self._activate)
        self.free = activation.Free(self.freed.append)

    def _activate(self, name, fds, count):
        self.names.append(name)
        if self.err:
            return self.err
        fds[0] = ctypes.cast(self.array, ctypes.POINTER(ctypes.c_int))
        count[0] = self.count
        return 0

    def install(self, monkeypatch):
        monkeypatch.setattr(activation, "_launchd_api", lambda: (self.activate, self.free))
        return self


def is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def test_launchd_hands_over_its_socket_and_the_array_is_freed(monkeypatch):
    with listening_socket() as sock:
        fd = os.dup(sock.fileno())
        os.set_inheritable(fd, True)
        fake = FakeLaunchd([fd]).install(monkeypatch)
        got = activation.inherited_socket({"WEBSPEC_LAUNCHD_SOCKET": "gateway"})
        try:
            assert got.source == "launchd" and got.socket.fileno() == fd
            assert got.address == "127.0.0.1:%d" % sock.getsockname()[1]
            assert not os.get_inheritable(fd)
            assert fake.names == [b"gateway"]
            assert fake.freed == [ctypes.addressof(fake.array)]
        finally:
            got.socket.close()


@pytest.mark.parametrize("err, hint", [
    (errno.ENOENT, "the job has no socket by that name: WEBSPEC_LAUNCHD_SOCKET must name a key of its "
                   "Sockets dictionary"),
    # launchd answers ESRCH to a job that asks for a name its Sockets dictionary lacks, not
    # ENOENT as launch(3) says (seen on macOS 27), so the hint names both causes.
    (errno.ESRCH, "this process is not a launchd job, or its job has no socket by that name: "
                  "WEBSPEC_LAUNCHD_SOCKET must name a key of the job's Sockets dictionary, and the job must "
                  "exec the gateway, not start it as a child"),
    (errno.EALREADY, "the socket was already handed over"),
    (errno.EPERM, None),
])
def test_launchd_errors_are_reported(monkeypatch, err, hint):
    fake = FakeLaunchd(err=err).install(monkeypatch)
    with pytest.raises(activation.ActivationError) as exc:
        activation.inherited_socket({"WEBSPEC_LAUNCHD_SOCKET": "gateway"})
    message = f"launch_activate_socket('gateway') failed: {os.strerror(err)}"
    assert str(exc.value) == (message if hint is None else f"{message} ({hint})")
    assert fake.freed == []  # nothing was allocated


@pytest.mark.parametrize("count", [0, 2])
def test_launchd_must_pass_exactly_one_socket(monkeypatch, count):
    socks = [listening_socket() for _ in range(count)]
    fds = [os.dup(s.fileno()) for s in socks]
    fake = FakeLaunchd(fds).install(monkeypatch)
    try:
        with pytest.raises(activation.ActivationError, match=f"passed {count} sockets named 'gateway'"):
            activation.inherited_socket({"WEBSPEC_LAUNCHD_SOCKET": "gateway"})
        assert fake.freed == [ctypes.addressof(fake.array)]
        assert not any(is_open(fd) for fd in fds)  # every descriptor it was handed is closed
    finally:
        for s in socks:
            s.close()


def test_launchd_socket_must_be_tcp(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind(("127.0.0.1", 0))
        fd = os.dup(udp.fileno())
        FakeLaunchd([fd]).install(monkeypatch)
        with pytest.raises(activation.ActivationError, match="not a TCP stream socket"):
            activation.inherited_socket({"WEBSPEC_LAUNCHD_SOCKET": "gateway"})
        assert not is_open(fd)


def test_an_empty_launchd_socket_name_is_refused(monkeypatch):
    fake = FakeLaunchd().install(monkeypatch)
    with pytest.raises(activation.ActivationError, match="WEBSPEC_LAUNCHD_SOCKET is empty"):
        activation.inherited_socket({"WEBSPEC_LAUNCHD_SOCKET": ""})
    assert fake.names == []


@pytest.mark.skipif(sys.platform == "darwin", reason="launchd exists here")
def test_launchd_activation_off_macos_is_refused():
    with pytest.raises(activation.ActivationError, match="macOS only"):
        activation.inherited_socket({"WEBSPEC_LAUNCHD_SOCKET": "gateway"})


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_the_real_launch_activate_socket_refuses_a_process_launchd_did_not_start():
    # Read-only: launchd answers that this process (a test runner) has no such socket. Whatever
    # errno it picks, ESRCH or ENOENT, the hint points at the name and its Sockets dictionary.
    with pytest.raises(activation.ActivationError) as exc:
        activation.inherited_socket({"WEBSPEC_LAUNCHD_SOCKET": "webspec-test-none"})
    assert str(exc.value).startswith("launch_activate_socket('webspec-test-none') failed")
    assert "has no socket by that name: WEBSPEC_LAUNCHD_SOCKET must name a key of" in str(exc.value)


# ── Addresses ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("family, host, loopback", [
    (socket.AF_INET, "127.0.0.1", True),
    (socket.AF_INET, "127.3.2.1", True),
    (socket.AF_INET, "0.0.0.0", False),
    (socket.AF_INET, "10.0.0.7", False),
    (socket.AF_INET6, "::1", True),
    (socket.AF_INET6, "::", False),
    (socket.AF_INET6, "::ffff:127.0.0.1", False),  # contract: 127.0.0.0/8 or ::1
    (socket.AF_INET6, "fe80::1%lo0", False),
])
def test_loopback_addresses(family, host, loopback):
    assert activation._loopback(family, host) is loopback


@pytest.mark.parametrize("value, host", [
    (None, "127.0.0.1"),
    ("", "127.0.0.1"),  # DP-8: an empty value (WEBSPEC_HOST= in gateway.env) is unset, not "every interface"
    ("  ", "127.0.0.1"),
    ("::1", "::1"),
    ("0.0.0.0", "0.0.0.0"),  # an explicit override still works
])
def test_bind_host(monkeypatch, value, host):
    if value is None:
        monkeypatch.delenv("WEBSPEC_HOST", raising=False)
    else:
        monkeypatch.setenv("WEBSPEC_HOST", value)
    assert gateway_main.resolve_bind_host() == host
