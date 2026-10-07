"""The compose stack is well-formed and hardened for DP-1 to DP-9 (docs/spec/audit-deployment.md),
the entrypoint sources the guard key from the vault, and the image's init (docker/init.py)
reaps orphans, forwards signals to the gateway, continues it when it is stopped, stays
non-dumpable, and with --listen holds the gateway's port, in both address families on a
wildcard address, until every other process in the container is gone."""
import errno
import importlib.util
import ipaddress
import itertools
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
import types
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # repo root
DOCKER = ROOT / "docker"
INIT = DOCKER / "init.py"
LINUX = sys.platform.startswith("linux")

needs_docker = pytest.mark.skipif(not shutil.which("docker"), reason="docker not installed")


def _pid1_environment_readable():
    """Whether the init, run as a child of this process, can read PID 1's environment, and so
    refuses to start: in a container whose PID 1 is a shell or the test runner of the same user."""
    if os.getpid() == 1:
        return True  # PID 1 is this process, which its children's user can read
    try:
        with open("/proc/1/environ", "rb"):
            return True
    except OSError:
        return False


PID1_READABLE = _pid1_environment_readable()


def test_entrypoint_sources_guard_key():
    text = (DOCKER / "entrypoint.sh").read_text()
    assert "OP_GUARD_KEY_REF" in text
    assert "op read" in text
    assert "WEBSPEC_GUARD_KEY" in text


def test_compose_declares_two_services(compose):
    # caddy and gateway run. The registry's block is kept but commented out on purpose
    # (docker/README.md, "The registry service"), so it must not parse as a service.
    assert set(compose["services"]) == {"caddy", "gateway"}
    text = (DOCKER / "docker-compose.yml").read_text()
    assert re.search(r"^  # registry:\n  #   build:\n", text, re.M)
    assert '#   command: ["python", "-m", "webspec_registry"]' in text


def _compose(*args, files=("docker-compose.yml",), env=None):
    """`docker compose -f ... ARGS`, with no WEBSPEC_* variable from the caller's shell."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("WEBSPEC_")}
    command = ["docker", "compose"]
    for name in files:
        command += ["-f", str(DOCKER / name)]
    return subprocess.run([*command, *args], capture_output=True, text=True, timeout=120,
                          env={**clean, **(env or {})})


@needs_docker
def test_compose_config_valid():
    r = _compose("config", "--format", "json")
    assert r.returncode == 0, r.stderr
    stack = json.loads(r.stdout)
    services = stack["services"]
    ports = [(p["host_ip"], str(p["published"]), p["target"]) for p in services["caddy"]["ports"]]
    assert ports == [("127.0.0.1", "7001", 7001), ("::1", "7001", 7001)]
    config = [(v["type"], v["source"], v.get("read_only")) for v in services["gateway"]["volumes"]
              if v["target"] == "/config"]
    assert config == [("bind", str(DOCKER / "config"), True)]
    # DP-2: an explicit false survives normalization; a missing key takes the daemon's default.
    assert services["gateway"]["init"] is False
    assert services["gateway"]["environment"]["WEBSPEC_CONFIG_HOST"] == ""
    # The same for IPv6 on the stack's network, which both services are on, and on no other.
    # Compose releases before 2.29 hold enable_ipv6 as a plain bool, false unless the file says
    # true, and leave a false out of their output; test_stack_network_is_ipv4_only checks the key
    # in the file itself.
    assert stack["networks"]["default"].get("enable_ipv6", False) is False
    assert {name: service["networks"] for name, service in services.items()} == {
        "caddy": {"default": None}, "gateway": {"default": None}}
    assert {name: service["stop_grace_period"] for name, service in services.items()} == {
        "caddy": "40s", "gateway": "40s"}


# ── Hardening ──


@pytest.fixture(scope="module")
def compose():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((DOCKER / "docker-compose.yml").read_text())


def test_only_caddy_is_published_and_only_on_loopback(compose):
    # DP-5: nothing off the host reaches the stack except through a tunnel in front of caddy.
    # Both loopback addresses: a local user who could listen on [::1]:7001 would receive the
    # requests of every client that tries ::1 first for *.localhost.
    assert compose["services"]["caddy"]["ports"] == ["127.0.0.1:7001:7001", "[::1]:7001:7001"]
    assert "ports" not in compose["services"]["gateway"]


def _ipv4_only_override():
    yaml = pytest.importorskip("yaml")

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))
    return yaml.load((DOCKER / "ipv4-only.yml").read_text(), Loader=Loader)


def test_ipv4_only_override_publishes_the_ipv4_loopback_alone():
    # For a host whose loopback has no IPv6, where Docker cannot bind [::1] and caddy would not
    # start. `!override` replaces the ports; without it, compose would add to them.
    assert _ipv4_only_override() == {"services": {"caddy": {"ports": ["127.0.0.1:7001:7001"]}}}
    assert re.search(r"^    ports: !override$", (DOCKER / "ipv4-only.yml").read_text(), re.M)


@needs_docker
def test_ipv4_only_override_replaces_the_published_ports():
    r = _compose("config", "--format", "json", files=("docker-compose.yml", "ipv4-only.yml"))
    assert r.returncode == 0, r.stderr
    ports = json.loads(r.stdout)["services"]["caddy"]["ports"]
    assert [(p["host_ip"], str(p["published"]), p["target"]) for p in ports] == [("127.0.0.1", "7001", 7001)]


def test_stack_network_is_ipv4_only(compose):
    # With a global IPv6 prefix on the stack's network, Docker's DNS also gives Caddy the
    # gateway's IPv6 address, which Caddy dials first, and where the gateway, on 0.0.0.0, does
    # not listen: whatever listened on [::]:7002 in its container would receive what Caddy
    # forwards. Explicitly false: without the key, the network takes the daemon's default, which
    # daemon.json can make IPv6. No service may join another network, which could have IPv6.
    assert compose["networks"] == {"default": {"enable_ipv6": False}}
    for name, service in compose["services"].items():
        assert "networks" not in service and "network_mode" not in service, name


def test_both_services_restart_unless_stopped(compose):
    # Without a policy the stack stays down after any exit, a daemon restart or a reboot.
    # on-failure would leave it down after an exit with status 0.
    for name in ("caddy", "gateway"):
        assert compose["services"][name].get("restart") == "unless-stopped", name


def test_both_services_have_time_to_finish_the_calls_in_flight(compose, monkeypatch):
    # Stopped, the gateway waits for the tool calls in progress, and its init then for the
    # container's other processes to be gone. `docker compose stop` stops Caddy first, which
    # waits for the answers it is passing on. Docker's default wait, 10 seconds or less, would
    # cut those calls short, or their answers.
    from webspec.__main__ import GRACEFUL_SHUTDOWN_SECONDS
    init = _load_init(monkeypatch)
    for name in ("caddy", "gateway"):
        assert compose["services"][name]["stop_grace_period"] == "40s", name
    assert 40 >= GRACEFUL_SHUTDOWN_SECONDS + init.END_EVERYONE_ELSE_SECONDS
    text = " ".join(_readme().split())
    assert ("Docker gives both services 40 seconds to stop (`stop_grace_period`) before it kills them. "
            f"The gateway waits up to {GRACEFUL_SHUTDOWN_SECONDS} seconds for the tool calls in progress, "
            "and Caddy, which `docker compose stop` stops first, for the answers it is passing on") in text


def test_gateway_runs_as_its_own_user(compose):
    # DP-1: the image's `webspec` user, never root.
    assert compose["services"]["gateway"]["user"] == "10001:10001"


def test_caddy_runs_as_another_unprivileged_user(compose):
    uid, _, gid = compose["services"]["caddy"]["user"].partition(":")
    assert uid not in ("", "0", "root", "10001")
    assert gid not in ("", "0", "root")


def test_containers_are_read_only_and_unprivileged(compose):
    services = compose["services"]
    for name in ("caddy", "gateway"):
        assert services[name]["read_only"] is True, name
        assert services[name]["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in services[name]["security_opt"], name
    assert "cap_add" not in services["gateway"]
    # caddy:2's binary carries the file capability cap_net_bind_service, which must be in the
    # bounding set for the kernel to exec it: without it the container cannot start ("exec
    # /usr/bin/caddy: operation not permitted"). Nothing else may come back.
    assert services["caddy"].get("cap_add") == ["NET_BIND_SERVICE"]


def test_caddy_reads_only_its_caddyfile(compose):
    # The Caddyfile imports nothing, so it is Caddy's whole configuration.
    assert compose["services"]["caddy"]["volumes"] == ["./caddy/Caddyfile:/etc/caddy/Caddyfile:ro"]


def test_gateway_state_lives_in_the_named_volume(compose):
    gateway = compose["services"]["gateway"]
    assert "webspec-state:/var/lib/webspec" in gateway["volumes"]
    assert "webspec-state" in compose["volumes"]
    assert gateway["environment"]["WEBSPEC_AUDIT_LOG"] == "/var/lib/webspec/gateway-audit.jsonl"
    assert any(mount.split(":")[0] == "/tmp" for mount in gateway["tmpfs"])


def test_gateway_config_is_a_read_only_directory_mount(compose):
    # DP-4: neither the gateway nor the stdio MCP servers it runs can write its configuration.
    # A directory, not the file: a file mount pins the file's inode, so an edit that replaces
    # the file (webspec-ctl, sed -i, mv) would never reach the running gateway.
    gateway = compose["services"]["gateway"]
    assert gateway["environment"]["WEBSPEC_CONFIG"] == "/config/claude.json"
    assert [v for v in gateway["volumes"] if "/config" in v] == ["${WEBSPEC_CONFIG_DIR:-./config}:/config:ro"]
    assert json.loads((DOCKER / "config" / "claude.json").read_text()) == {"mcpServers": {}}
    assert sorted(p.name for p in (DOCKER / "config").iterdir()) == ["claude.json"]


@needs_docker
def test_compose_mounts_the_directory_it_is_given():
    r = _compose("config", "--format", "json", env={"WEBSPEC_CONFIG_DIR": "/srv/webspec-test"})
    assert r.returncode == 0, r.stderr
    volumes = json.loads(r.stdout)["services"]["gateway"]["volumes"]
    assert [v["source"] for v in volumes if v["target"] == "/config"] == ["/srv/webspec-test"]


def test_gateway_gets_the_retired_config_variable_to_refuse_it(compose):
    # DP-4: earlier versions mounted the file WEBSPEC_CONFIG_HOST named, typically a protected
    # one. Ignored, an upgraded stack would silently serve the checkout's docker/config/ in its
    # place. Compose hands it to the entrypoint, which refuses to start while it is set. Plain
    # ${VAR:-}: Compose 2.x evaluates a nested ${A:+${B:?}} eagerly and fails every command.
    environment = compose["services"]["gateway"]["environment"]
    assert environment["WEBSPEC_CONFIG_HOST"] == "${WEBSPEC_CONFIG_HOST:-}"


@needs_docker
def test_compose_passes_the_retired_config_variable_on_and_mounts_no_file():
    r = _compose("config", "--format", "json", env={"WEBSPEC_CONFIG_HOST": "/etc/webspec/config.json"})
    assert r.returncode == 0, r.stderr
    gateway = json.loads(r.stdout)["services"]["gateway"]
    assert gateway["environment"]["WEBSPEC_CONFIG_HOST"] == "/etc/webspec/config.json"
    assert [v["target"] for v in gateway["volumes"]] == ["/config", "/var/lib/webspec"]


def test_compose_leaves_pid_1_to_the_images_init(compose):
    # DP-2: with `init: true`, docker-init would be PID 1, running as uid 10001 and dumpable, so
    # the stdio MCP servers (same user) could read the container's environment, guard key
    # included, from /proc/1/environ. The image's own init makes itself non-dumpable instead.
    # Explicitly false: without the key, a daemon.json with "init": true would add docker-init.
    gateway = compose["services"]["gateway"]
    assert gateway["init"] is False
    assert "entrypoint" not in gateway
    # The registry's commented-out block runs the same image, and so the same init.
    assert re.search(r"^  #   init: false\b", (DOCKER / "docker-compose.yml").read_text(), re.M)


def test_one_gateway_process(compose):
    # DP-7: nonces, pins, clearances, approvals and idempotency records live in one process.
    gateway = compose["services"]["gateway"]
    assert gateway.get("deploy", {}).get("replicas", 1) == 1
    assert "scale" not in gateway


def test_image_drops_root():
    text = (DOCKER / "Dockerfile").read_text()
    assert "--uid 10001" in text and "--gid 10001" in text
    assert "--shell /usr/sbin/nologin" in text
    assert "-m 0700 /var/lib/webspec" in text
    assert "WEBSPEC_AUDIT_LOG=/var/lib/webspec/gateway-audit.jsonl" in text
    assert "\nUSER 10001:10001\n" in text


def test_image_runs_its_own_init_as_pid_1():
    # PID 1 reaps the orphans stdio servers leave behind, and must not be readable by them.
    text = (DOCKER / "Dockerfile").read_text()
    assert "\nCOPY docker/init.py /usr/local/libexec/webspec-init.py\n" in text
    entrypoint = json.loads(re.search(r"^ENTRYPOINT (\[.*\])$", text, re.M).group(1))
    assert entrypoint == ["/usr/local/bin/python3", "-I", "/usr/local/libexec/webspec-init.py", "--listen",
                          "/usr/local/bin/entrypoint.sh"]


def test_gateway_python_ignores_user_site_packages(compose):
    # HOME is the state volume, which the stdio MCP servers (same uid) can write. With user
    # site-packages on, a .pth file they plant under ~/.local would run inside the gateway,
    # guard key in reach, at its next start.
    text = (DOCKER / "Dockerfile").read_text()
    assert re.search(r"^\s+PYTHONNOUSERSITE=1\b", text, re.M)
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", text, re.M).group(1))
    assert cmd[0] == "python" and cmd[cmd.index("-m") + 1] == "webspec"
    assert {"-I", "-s"} & set(cmd[:cmd.index("-m")])
    # Compose must not swap the command for one without the flag, or clear the variable.
    gateway = compose["services"]["gateway"]
    assert "command" not in gateway and "entrypoint" not in gateway
    assert "PYTHONNOUSERSITE" not in gateway["environment"]


# ── docker/entrypoint.sh ──


def _entrypoint(env, *command):
    return subprocess.run([shutil.which("sh"), str(DOCKER / "entrypoint.sh"), *command],
                          env=env, capture_output=True, text=True, timeout=30)


def _stub_op_path(tmp_path):
    """A PATH whose `op` is a stub that only says it ran, so no test ever reaches a real vault CLI."""
    stub = tmp_path / "stub-bin" / "op"
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\necho 'stub op ran' >&2\nexit 1\n")
    stub.chmod(0o755)
    return f"{stub.parent}{os.pathsep}{os.environ['PATH']}"


def test_entrypoint_refuses_to_start_when_op_is_missing(tmp_path):
    # GD-5: a gateway whose key was meant to come from the vault never boots without it.
    r = _entrypoint({"PATH": str(tmp_path), "OP_GUARD_KEY_REF": "op://vault/item/key"}, "/bin/echo", "started")
    assert r.returncode == 1
    assert "OP_GUARD_KEY_REF is set" in r.stderr and "started" not in r.stdout


def test_entrypoint_refuses_the_retired_config_variable(tmp_path):
    # DP-4: an upgraded stack that still names its protected config file in WEBSPEC_CONFIG_HOST
    # must not start on the checkout's docker/config/ instead. It fails before it reads a secret.
    env = {"PATH": _stub_op_path(tmp_path), "WEBSPEC_CONFIG_HOST": "/etc/webspec/config.json",
           "OP_GUARD_KEY_REF": "op://vault/item/key"}
    r = _entrypoint(env, "/bin/echo", "started")
    assert r.returncode == 1 and r.stdout == ""
    assert "WEBSPEC_CONFIG_HOST (/etc/webspec/config.json) is retired" in r.stderr
    assert "WEBSPEC_CONFIG_DIR" in r.stderr and "stub op ran" not in r.stderr
    # Compose passes it on empty when it is unset.
    r = _entrypoint({"PATH": os.environ["PATH"], "WEBSPEC_CONFIG_HOST": ""}, "/bin/echo", "started")
    assert (r.returncode, r.stdout, r.stderr) == (0, "started\n", "")


def test_entrypoint_says_why_the_gateway_has_no_services(tmp_path):
    (tmp_path / "config").mkdir()
    env = {"PATH": os.environ["PATH"], "WEBSPEC_CONFIG": str(tmp_path / "config" / "claude.json")}
    r = _entrypoint(env, "/bin/echo", "started")
    assert r.returncode == 0 and r.stdout == "started\n"
    # The gateway picks the file up within 30 seconds of its appearing.
    assert f"cannot find {tmp_path}/config/claude.json; no services until it appears" in r.stderr
    (tmp_path / "config" / "claude.json").write_text("{}")
    assert _entrypoint(env, "true").stderr == ""
    # A container without the directory, like the audit-log ones, stays quiet.
    env["WEBSPEC_CONFIG"] = str(tmp_path / "absent" / "claude.json")
    assert _entrypoint(env, "true").stderr == ""


def test_entrypoint_refuses_a_config_directory_that_is_a_file(tmp_path):
    # WEBSPEC_CONFIG_DIR naming the file, not its directory, mounts that file at /config: the
    # gateway could never read /config/claude.json, whatever happens next. It fails before it
    # reads a secret.
    (tmp_path / "config").write_text('{"mcpServers": {}}')
    env = {"PATH": _stub_op_path(tmp_path), "WEBSPEC_CONFIG": str(tmp_path / "config" / "claude.json"),
           "OP_GUARD_KEY_REF": "op://vault/item/key"}
    r = _entrypoint(env, "/bin/echo", "started")
    assert (r.returncode, r.stdout) == (1, "")
    assert r.stderr == (f"entrypoint: {tmp_path}/config is not a directory, so {tmp_path}/config/claude.json can "
                        "never be read. WEBSPEC_CONFIG_DIR must name the directory that holds claude.json, not "
                        'the file (docker/README.md, "Service configuration")\n')
    # A file directly under / or the working directory has a directory to live in.
    for config in ("/claude.json", "claude.json"):
        r = _entrypoint({"PATH": os.environ["PATH"], "WEBSPEC_CONFIG": config}, "/bin/echo", "started")
        assert (r.returncode, r.stdout) == (0, "started\n"), config


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads and searches whatever the mode says")
def test_entrypoint_says_what_brings_back_an_unreadable_config(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "claude.json").write_text("{}")
    env = {"PATH": os.environ["PATH"], "WEBSPEC_CONFIG": str(config / "claude.json")}
    try:
        # chmod and chown change the file's change time, which the gateway's reload compares
        # (webspec/config.py, ServiceRegistry), so fixing the mode is enough.
        (config / "claude.json").chmod(0)
        r = _entrypoint(env, "/bin/echo", "started")
        assert r.returncode == 0 and r.stdout == "started\n"
        assert f"cannot read {config}/claude.json; no services. Make it readable by uid {os.getuid()}; " \
               "the gateway picks it up within 30 seconds" in r.stderr
        # A directory it cannot search hides the file, which the gateway then picks up as it would
        # a new one, once it can.
        (config / "claude.json").chmod(0o644)
        config.chmod(0o600)
        r = _entrypoint(env, "true")
        assert f"cannot search {config}; no services until uid {os.getuid()} can" in r.stderr
        assert "cannot find" not in r.stderr and "restart" not in r.stderr
    finally:
        config.chmod(0o755)
        (config / "claude.json").chmod(0o644)


# ── docker/init.py: PID 1 of the gateway container ──


def _load_init(monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # no __pycache__ in docker/
    spec = importlib.util.spec_from_file_location("webspec_docker_init", INIT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# supervise() is all of the init but the Linux-only prctl, so it runs on every platform.
_SUPERVISE = (
    "import importlib.util, sys\n"
    f"spec = importlib.util.spec_from_file_location('webspec_docker_init', {str(INIT)!r})\n"
    "init = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(init)\n"
    "sys.exit(init.supervise(sys.argv[1:]))\n"
)


def _supervise(*argv, timeout=30, **kwargs):
    # In a process group of its own, the init is forked and exec'd, as runc starts a container's
    # first process. Python 3.13 and later otherwise start it with posix_spawn, and glibc's
    # posix_spawn, where it cannot use clone3 (Docker's default seccomp profile refuses it),
    # leaves the library's internal signals 32 and 33 ignored in the new program. Python cannot
    # reset those (they are not in signal.valid_signals()), so the init would pass them on to its
    # child (test_init_starts_its_child_with_default_signal_handling).
    return subprocess.run([sys.executable, "-B", "-c", _SUPERVISE, *argv], timeout=timeout, process_group=0,
                          **kwargs)


def _readline(stream, timeout=10.0):
    ready, _, _ = select.select([stream], [], [], timeout)
    assert ready, "no output in time"
    return stream.readline()


def test_init_exits_with_its_childs_exit_code():
    assert _supervise("sh", "-c", "exit 7").returncode == 7


def test_init_reports_death_by_signal_as_128_plus_the_signal():
    assert _supervise("sh", "-c", "kill -KILL $$").returncode == 128 + signal.SIGKILL


def test_init_reports_a_command_it_cannot_start():
    r = _supervise("/nonexistent/command", capture_output=True, text=True)
    assert r.returncode == 127
    assert "cannot start /nonexistent/command" in r.stderr


def test_init_forwards_sigterm_to_its_child():
    # `docker stop` signals PID 1. The gateway must get the signal to shut down cleanly.
    child = 'trap "echo got TERM; exit 3" TERM; echo ready; while :; do sleep 0.1; done'
    with subprocess.Popen([sys.executable, "-B", "-c", _SUPERVISE, "sh", "-c", child],
                          stdout=subprocess.PIPE, text=True) as proc:
        try:
            assert _readline(proc.stdout) == "ready\n"
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=30) == 3
            assert proc.stdout.read() == "got TERM\n"
        finally:
            proc.kill()


def test_init_continues_its_child_when_a_signal_stops_it():
    # The gateway is not PID 1, so the stdio servers (same user) can stop it: the kernel drops
    # SIGSTOP only for PID 1. The container would stay up, serving nothing, and never restart.
    with subprocess.Popen([sys.executable, "-B", "-c", _SUPERVISE,
                           "sh", "-c", "kill -STOP $$; echo resumed; exit 5"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          start_new_session=True) as proc:
        try:
            out, err = proc.communicate(timeout=15)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # the stopped shell too, if the init failed
            except ProcessLookupError:
                pass
    assert (proc.returncode, out) == (5, "resumed\n")
    assert re.fullmatch(r"webspec-init: child \d+ stopped by SIGSTOP; continuing it\n", err)


@pytest.mark.skipif(not LINUX, reason="orphans reach the init through Linux's child subreaper")
def test_init_leaves_a_stopped_orphan_alone():
    # Only its own child, the gateway, is the init's to continue. A stopped orphan is reported
    # too; it must neither be taken for the gateway nor break the loop.
    child = textwrap.dedent("""\
        import os, signal, sys, time
        init = os.getppid()
        r, w = os.pipe()
        if os.fork() == 0:
            if os.fork() == 0:          # the orphan
                os.write(w, str(os.getpid()).encode())
                while os.getppid() != init:
                    time.sleep(0.01)    # stop once the init is its parent
                os.kill(os.getpid(), signal.SIGSTOP)
                os._exit(0)
            os._exit(0)
        os.wait()
        orphan = int(os.read(r, 16))
        time.sleep(1)                   # the init has seen the orphan stop by now
        with open(f"/proc/{orphan}/stat") as f:
            print("orphan", f.read().rsplit(")", 1)[1].split()[0], flush=True)
        os.kill(orphan, signal.SIGCONT)
        time.sleep(1)
        print("orphan gone", not os.path.exists(f"/proc/{orphan}"), flush=True)
    """)
    r = _supervise(sys.executable, "-c", child, capture_output=True, text=True)
    assert (r.returncode, r.stderr) == (0, "")
    assert r.stdout.splitlines() == ["orphan T", "orphan gone True"]


def test_init_starts_nothing_when_it_cannot_be_non_dumpable(monkeypatch):
    # It holds the container's environment, guard key included (DP-2).
    init = _load_init(monkeypatch)
    started = []

    def refuse():
        raise OSError(errno.ENOSYS, "prctl unavailable")

    monkeypatch.setattr(init, "make_non_dumpable", refuse)
    monkeypatch.setattr(init, "supervise", lambda argv: started.append(argv) or 0)
    assert init.main(["true"]) == 1
    assert init.main([]) == 2
    assert started == []


@pytest.mark.skipif(os.getpid() == 1, reason="PID 1 never refuses itself")
def test_init_starts_nothing_under_another_pid_1_it_can_read(monkeypatch, tmp_path, capsys):
    # DP-2: docker-init (`docker run --init`, compose's `init: true`, or a daemon-wide
    # "init": true) is PID 1 as the container's user and dumpable, and holds the container's
    # environment, guard key included: every stdio server could read it from /proc/1/environ.
    init = _load_init(monkeypatch)
    started = []
    monkeypatch.setattr(init, "make_non_dumpable", lambda: None)
    monkeypatch.setattr(init, "supervise", lambda argv: started.append(argv) or 0)
    (tmp_path / "environ").write_bytes(b"WEBSPEC_GUARD_KEY=not-a-real-key\0")
    monkeypatch.setattr(init, "PID1_ENVIRON", str(tmp_path / "environ"))
    assert init.main(["entrypoint.sh"]) == 1
    assert started == []
    err = capsys.readouterr().err
    assert err.startswith("webspec-init: refusing to start entrypoint.sh: PID 1 (unknown) is another init")
    assert "(DP-2)" in err and "init: false" in err and "not-a-real-key" not in err
    # Outside a container PID 1 is root's, and its environment cannot be opened.
    monkeypatch.setattr(init, "PID1_ENVIRON", str(tmp_path / "unreadable"))
    assert init.main(["entrypoint.sh"]) == 0
    assert started == [["entrypoint.sh"]]


@pytest.mark.skipif(not PID1_READABLE, reason="PID 1's environment is closed to this user, as it should be")
def test_init_refuses_to_run_here_because_pid_1_is_readable():
    # Seen in a container whose PID 1 is a shell or the test runner, of the same user.
    r = subprocess.run([sys.executable, "-I", str(INIT), "sh", "-c", "echo started"],
                       capture_output=True, text=True, timeout=30)
    assert (r.returncode, r.stdout) == (1, "")
    assert "is another init, and this user can read its environment" in r.stderr


@pytest.mark.skipif(not LINUX, reason="orphans reach the init through Linux's child subreaper")
def test_init_reaps_orphans():
    # A process a stdio server leaves behind is reparented to PID 1, and stays a zombie until
    # the container restarts unless PID 1 waits for it. Outside a container the init adopts
    # orphans as a child subreaper.
    child = textwrap.dedent("""\
        import os, sys, time
        init = os.getppid()
        if os.fork() == 0:              # a helper of the stdio server...
            if os.fork() == 0:          # ...starts a process and exits without waiting for it
                time.sleep(0.3)
                print("orphan's parent", os.getppid() == init, flush=True)
                os._exit(0)
            os._exit(0)
        os.wait()
        time.sleep(2)                   # the orphan has exited by now
        zombies = []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{pid}/stat") as f:
                    state, ppid = f.read().rsplit(")", 1)[1].split()[:2]
            except OSError:
                continue
            if state == "Z" and int(ppid) == init:
                zombies.append(pid)
        print("zombies", zombies, flush=True)
    """)
    r = _supervise(sys.executable, "-c", child, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["orphan's parent True", "zombies []"]


@pytest.mark.skipif(not LINUX, reason="reads /proc/<pid>/status")
def test_init_starts_its_child_with_default_signal_handling():
    # Python ignores SIGPIPE and SIGXFSZ and the init blocks the signals it waits for. The
    # gateway, and every stdio server it starts, must inherit neither.
    r = _supervise("cat", "/proc/self/status", capture_output=True, text=True)
    fields = dict(line.split(":", 1) for line in r.stdout.splitlines() if ":" in line)
    assert int(fields["SigBlk"], 16) == 0
    assert int(fields["SigIgn"], 16) == 0


@pytest.mark.skipif(not LINUX, reason="prctl(PR_SET_DUMPABLE) is Linux-only")
@pytest.mark.skipif(LINUX and os.geteuid() == 0, reason="root can read any process's environment")
@pytest.mark.skipif(PID1_READABLE, reason="the init refuses to start under this PID 1")
def test_init_is_non_dumpable():
    # DP-2: its child stands in for a stdio MCP server, which runs as the same user.
    child = textwrap.dedent("""\
        import os, sys
        try:
            open(f"/proc/{os.getppid()}/environ", "rb").read()
        except PermissionError:
            sys.exit(0)
        sys.exit(1)
    """)
    r = subprocess.run([sys.executable, "-I", str(INIT), sys.executable, "-c", child],
                       env={**os.environ, "WEBSPEC_GUARD_KEY": "not-a-real-key"}, timeout=30)
    assert r.returncode == 0


@pytest.mark.skipif(not LINUX, reason="prctl(PR_SET_DUMPABLE) is Linux-only")
@pytest.mark.skipif(PID1_READABLE, reason="the init refuses to start under this PID 1")
def test_init_runs_its_command():
    r = subprocess.run([sys.executable, "-I", str(INIT), "--", "sh", "-c", "echo started; exit 4"],
                       capture_output=True, text=True, timeout=30)
    assert (r.returncode, r.stdout) == (4, "started\n")


# ── docker/init.py --listen: the init holds the gateway's port ──
#
# The image passes --listen. The gateway takes the socket because it is bound to the address
# WEBSPEC_HOST names (0.0.0.0 in the stack), DP-8's explicit override (webspec/activation.py).


_START_LISTENING = (
    "import importlib.util, sys\n"
    f"spec = importlib.util.spec_from_file_location('webspec_docker_init', {str(INIT)!r})\n"
    "init = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(init)\n"
    "sys.exit(init.start(sys.argv[1:], listen=True))\n"
)


def _listening_init(child, host="127.0.0.1", port="0"):
    env = {**os.environ, "WEBSPEC_HOST": host, "WEBSPEC_INTERNAL_PORT": port}
    return subprocess.Popen([sys.executable, "-B", "-c", _START_LISTENING, sys.executable, "-c", child], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _can_bind(host):
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
            s.bind((host, 0))
        return True
    except OSError:
        return False


def _bind_errno(host, port):
    """0 if a new socket, with no option but IPV6_V6ONLY, can bind host:port now, else why not."""
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        if ":" in host:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            return e.errno
    return 0


def _join(host, port):
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _port_free_in_both_families():
    """A port free on 0.0.0.0 and on [::] alike, for an init that listens on one and holds the other."""
    for _ in range(20):
        with socket.socket() as v4, socket.socket(socket.AF_INET6) as v6:
            v4.bind(("0.0.0.0", 0))
            port = v4.getsockname()[1]
            v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                v6.bind(("::", port))
            except OSError:
                continue
        return port
    pytest.skip("no port is free in both address families")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_init_hands_its_child_the_socket_as_systemd_does(host):
    # sd_listen_fds(3), which webspec/activation.py follows: descriptor 3, LISTEN_FDS=1, and
    # LISTEN_PID naming the process that takes it, which the entrypoint's exec keeps.
    if not _can_bind(host):
        pytest.skip(f"{host} is not configured here")
    child = textwrap.dedent("""\
        import os, socket
        assert os.environ["LISTEN_FDS"] == "1" and os.environ["LISTEN_PID"] == str(os.getpid())
        assert os.environ["LISTEN_FDNAMES"] == "webspec-init"  # the gateway logs it as the holder
        sock = socket.socket(fileno=3)
        assert sock.type == socket.SOCK_STREAM
        print(sock.getsockname()[1], flush=True)
        conn, _ = sock.accept()
        conn.sendall(b"served by the child")
        conn.close()
    """)
    with _listening_init(child, host=host) as proc:
        try:
            port = int(_readline(proc.stdout))
            with socket.create_connection((host, port), timeout=10) as client:
                assert client.recv(64) == b"served by the child"
            assert proc.wait(timeout=30) == 0
            # Bound to one address, not a wildcard: nothing is held in the other family.
            assert proc.stderr.read() == (f"webspec-init: listening on {_join(host, port)} for {sys.executable}; "
                                          "the port stays bound until this init exits\n")
        finally:
            proc.kill()


def test_init_keeps_the_port_bound_while_its_child_shuts_down():
    # On SIGTERM uvicorn closes its listening socket first, then waits up to a tool call's 30
    # seconds for the requests in progress. Bound by the gateway alone, the port would be free
    # all that time, and a stdio server (same user) could listen on it and receive what Caddy
    # forwards there for every destination.
    child = textwrap.dedent("""\
        import socket, sys
        sock = socket.socket(fileno=3)
        port = sock.getsockname()[1]
        sock.close()            # as uvicorn does first when it shuts down
        print(port, "closed", flush=True)
        sys.stdin.readline()    # ...then it waits for the requests in progress
    """)
    with _listening_init(child) as proc:
        try:
            port, closed = _readline(proc.stdout).split()
            port = int(port)
            assert closed == "closed"
            options = [[], [socket.SO_REUSEADDR]] + ([[socket.SO_REUSEPORT]] if hasattr(socket, "SO_REUSEPORT") else [])
            for option in options:
                with socket.socket() as squatter:
                    for name in option:
                        squatter.setsockopt(socket.SOL_SOCKET, name, 1)
                    with pytest.raises(OSError) as refused:
                        squatter.bind(("127.0.0.1", port))
                    assert refused.value.errno == errno.EADDRINUSE, option
            # A connection still lands in the init's backlog, where nobody reads it.
            socket.create_connection(("127.0.0.1", port), timeout=10).close()
            proc.stdin.write("done\n")
            proc.stdin.flush()
            assert proc.wait(timeout=30) == 0
        finally:
            proc.kill()
    # The port is free once the init has exited.
    with socket.socket() as later:
        later.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        later.bind(("127.0.0.1", port))


def _as_pid_1():
    """A command that runs a program as PID 1 of a new PID namespace, as the init runs in its
    container; None where this user cannot make one (no unprivileged user namespaces, or a
    seccomp filter, such as Docker's default, that refuses them)."""
    unshare = shutil.which("unshare")
    if not (LINUX and unshare):
        return None
    command = [unshare, "--user", "--pid", "--fork", "--kill-child"]
    try:
        r = subprocess.run([*command, sys.executable, "-c", "import os; print(os.getpid())"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return command if (r.returncode, r.stdout) == (0, "1\n") else None


# The init as PID 1, here taking a second to exit once start() has returned: the kernel kills the
# namespace's other processes only once PID 1 has exited, however long that takes.
_START_LISTENING_AS_PID_1 = (
    "import importlib.util, sys, time\n"
    f"spec = importlib.util.spec_from_file_location('webspec_docker_init', {str(INIT)!r})\n"
    "init = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(init)\n"
    "code = init.start(sys.argv[1:], listen=True)\n"
    "time.sleep(1)\n"
    "sys.exit(code)\n"
)


@pytest.mark.skipif(not LINUX, reason="PID namespaces are Linux's")
def test_init_ends_every_other_process_before_it_lets_the_port_go():
    # The kernel kills a container's other processes only once PID 1 has exited, after it has
    # closed the gateway's socket. A process a stdio server left behind, which outlives the
    # gateway, could take the port in between, and receive what Caddy forwards there for every
    # destination. As PID 1, the init kills it first: the port stays bound until it is gone.
    # An init that killed it only just after letting the port go would pass here too, as the gap
    # is too short for this test to see; test_init_ends_everyone_else_while_it_still_holds_the_port
    # checks the order.
    pid_1 = _as_pid_1()
    if pid_1 is None:
        pytest.skip("this user cannot run a process as PID 1 of a new PID namespace (unshare --user --pid)")
    child = textwrap.dedent("""\
        import os, socket, time
        sock = socket.socket(fileno=3)
        port = sock.getsockname()[1]
        if os.fork() == 0:              # left behind by a stdio server, which never has the socket
            sock.close()
            print(port, flush=True)
            while True:                 # takes the port as soon as it is free
                with socket.socket() as squatter:
                    try:
                        squatter.bind(("127.0.0.1", port))
                    except OSError:
                        time.sleep(0.001)
                        continue
                    squatter.listen()
                    print("bound", flush=True)
                    time.sleep(60)
        time.sleep(0.5)                 # then the gateway exits, and that process outlives it
    """)
    env = {**os.environ, "WEBSPEC_HOST": "127.0.0.1", "WEBSPEC_INTERNAL_PORT": "0"}
    r = subprocess.run([*pid_1, sys.executable, "-B", "-c", _START_LISTENING_AS_PID_1, sys.executable, "-c", child],
                       env=env, capture_output=True, text=True, timeout=60)
    port = r.stdout.split("\n", 1)[0]
    assert port.isdigit(), (r.returncode, r.stdout, r.stderr)
    assert (r.returncode, r.stdout) == (0, f"{port}\n"), r.stderr  # the child's code, and never "bound"
    assert r.stderr == (f"webspec-init: listening on 127.0.0.1:{port} for {sys.executable}; the port stays "
                        "bound until this init exits\n")
    # The port is free once the init has exited.
    with socket.socket() as later:
        later.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        later.bind(("127.0.0.1", int(port)))


@pytest.mark.parametrize("host", [
    "127.0.0.1",
    pytest.param("0.0.0.0", marks=pytest.mark.skipif(
        not LINUX, reason="an all-interfaces listener can prompt the macOS firewall; the image runs on Linux")),
])
def test_init_ends_everyone_else_while_it_still_holds_the_port(monkeypatch, host):
    # P1's order, on every platform. Killed only once the init has let the port go, a process left
    # behind could take the port in between, however short the gap: in the compose stack, squatters
    # looping on bind() won most such races. So the init kills the others after the gateway has
    # exited, while every address it holds is still bound, and also when supervising fails.
    init = _load_init(monkeypatch)
    monkeypatch.setenv("WEBSPEC_HOST", host)
    monkeypatch.setenv("WEBSPEC_INTERNAL_PORT", str(_port_free_in_both_families() if host == "0.0.0.0" else 0))
    held, events = [], []

    def keeping(make):
        def keep(*args):
            sock = make(*args)
            if sock is not None:
                held.append(sock.getsockname()[:2])
            return sock
        return keep

    monkeypatch.setattr(init, "listening_socket", keeping(init.listening_socket))
    monkeypatch.setattr(init, "holding_socket", keeping(init.holding_socket))
    monkeypatch.setattr(init, "end_everyone_else",
                        lambda command: events.append(("end", command, [_bind_errno(*a) for a in held])))

    def supervise(argv, sock):
        events.append(("supervise", argv))
        return 7

    monkeypatch.setattr(init, "supervise", supervise)
    assert init.start(["entrypoint.sh"], listen=True) == 7  # the child's code, unchanged
    assert len(held) == (2 if host == "0.0.0.0" and _can_bind("::") else 1)
    assert events == [("supervise", ["entrypoint.sh"]), ("end", "entrypoint.sh", [errno.EADDRINUSE] * len(held))]
    assert [_bind_errno(*address) for address in held] == [0] * len(held)  # then it lets the port go

    def failing(argv, sock):
        events.append(("supervise", argv))
        raise RuntimeError("supervising failed")

    del held[:], events[:]
    monkeypatch.setattr(init, "supervise", failing)
    with pytest.raises(RuntimeError, match="supervising failed"):
        init.start(["entrypoint.sh"], listen=True)
    assert events == [("supervise", ["entrypoint.sh"]), ("end", "entrypoint.sh", [errno.EADDRINUSE] * len(held))]
    assert [_bind_errno(*address) for address in held] == [0] * len(held)


class _FakeOS:
    """The os module as end_everyone_else sees it, so that no test can reach the real kill(-1).

    kill(-1) finds someone `alive` more times, then no one; waitpid reaps `zombies` one by one.
    """
    WNOHANG = os.WNOHANG

    def __init__(self, pid, alive, zombies=()):
        self.pid, self.alive, self.zombies, self.kills, self.reaped = pid, alive, list(zombies), [], []
        self.started = time.monotonic()

    def getpid(self):
        return self.pid

    def kill(self, pid, sig):
        self.kills.append((pid, sig))
        assert time.monotonic() - self.started < 5, "still killing after 5 seconds: no deadline"
        if not self.alive:
            raise ProcessLookupError(errno.ESRCH, "No such process")
        self.alive -= 1

    def waitpid(self, pid, options):
        assert (pid, options) == (-1, os.WNOHANG)
        if self.zombies:
            self.reaped.append(self.zombies.pop(0))
            return self.reaped[-1], 0
        if self.alive:
            return 0, 0
        raise ChildProcessError(errno.ECHILD, "No child processes")


def test_init_ends_everyone_else_as_pid_1_alone_and_in_time(monkeypatch, capsys):
    init = _load_init(monkeypatch)
    # Anywhere but as PID 1, kill(-1) would reach every process of the init's user.
    fake = _FakeOS(pid=4242, alive=float("inf"))
    monkeypatch.setattr(init, "os", fake)
    init.end_everyone_else("entrypoint.sh", seconds=0.05)
    assert fake.kills == []
    # As PID 1: SIGKILL to every other process, and reap them, until none is left.
    fake = _FakeOS(pid=1, alive=2, zombies=[15, 16])
    monkeypatch.setattr(init, "os", fake)
    init.end_everyone_else("entrypoint.sh", seconds=5)
    assert fake.kills == [(-1, signal.SIGKILL)] * 3 and fake.reaped == [15, 16]
    assert capsys.readouterr().err == ""
    # Or until the deadline, should one outlive SIGKILL or belong to a user it cannot signal.
    fake = _FakeOS(pid=1, alive=float("inf"))
    monkeypatch.setattr(init, "os", fake)
    init.end_everyone_else("entrypoint.sh", seconds=0.05)
    assert len(fake.kills) > 1
    assert capsys.readouterr().err == ("webspec-init: other processes are still running 0.05 seconds after "
                                       "entrypoint.sh exited; letting its port go anyway\n")
    assert init.END_EVERYONE_ELSE_SECONDS == 2


@pytest.mark.skipif(not LINUX, reason="an all-interfaces listener can prompt the macOS firewall; the image runs on Linux")
@pytest.mark.parametrize("host, other", [("0.0.0.0", "::"), ("::", "0.0.0.0")])
def test_init_holds_the_port_in_the_other_address_family_too(host, other):
    # With the gateway on 0.0.0.0:7002 alone, a stdio server could listen on [::]:7002 and receive
    # what Caddy forwards to the gateway's IPv6 address, which Caddy dials first on a network with
    # a global IPv6 prefix. The init holds it too, without listening: such connections are
    # refused, and clients fall back to IPv4. The same the other way round.
    v6 = ":" in other
    held, served = ("::1", "127.0.0.1") if v6 else ("127.0.0.1", "::1")  # loopback, in each family
    if not all(_can_bind(address) for address in (host, other, held, served)):
        pytest.skip("this host lacks an address family")
    port = _port_free_in_both_families()
    child = 'import sys; print("ready", flush=True); sys.stdin.readline()'
    with _listening_init(child, host=host, port=str(port)) as proc:
        try:
            assert _readline(proc.stdout) == "ready\n"
            for address in (other, held):
                for option in ([], [socket.SO_REUSEADDR], [socket.SO_REUSEPORT]):
                    with socket.socket(socket.AF_INET6 if v6 else socket.AF_INET) as squatter:
                        if v6:
                            squatter.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                        for name in option:
                            squatter.setsockopt(socket.SOL_SOCKET, name, 1)
                        with pytest.raises(OSError) as refused:
                            squatter.bind((address, port))
                        assert refused.value.errno == errno.EADDRINUSE, (address, option)
            with pytest.raises(ConnectionRefusedError):
                socket.create_connection((held, port), timeout=10)
            socket.create_connection((served, port), timeout=10).close()  # into the gateway's backlog
            proc.stdin.write("done\n")
            proc.stdin.flush()
            assert proc.wait(timeout=30) == 0
            assert proc.stderr.read() == (f"webspec-init: listening on {_join(host, port)} for {sys.executable}, "
                                          f"and holding {_join(other, port)}; the port stays bound until this "
                                          "init exits\n")
        finally:
            proc.kill()


@pytest.mark.skipif(not LINUX, reason="an all-interfaces listener can prompt the macOS firewall; the image runs on Linux")
def test_init_starts_nothing_when_another_process_holds_the_other_family():
    # That process would receive the connections clients make there.
    if not (_can_bind("0.0.0.0") and _can_bind("::")):
        pytest.skip("this host lacks an address family")
    port = _port_free_in_both_families()
    with socket.socket(socket.AF_INET6) as taken:
        taken.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        taken.bind(("::", port))
        taken.listen()
        with _listening_init("print('started')", host="0.0.0.0", port=str(port)) as proc:
            out, err = proc.communicate(timeout=30)
    assert (proc.returncode, out) == (1, "")
    assert err == (f"webspec-init: refusing to start {sys.executable}: cannot hold [::]:{port} "
                   f"({os.strerror(errno.EADDRINUSE)})\n")


def test_init_starts_nothing_when_it_cannot_listen():
    # Never the gateway without the socket: it would bind the port itself.
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        with _listening_init("print('started')", port=str(port)) as proc:
            out, err = proc.communicate(timeout=30)
    assert (proc.returncode, out) == (1, "")
    assert err == (f"webspec-init: refusing to start {sys.executable}: cannot listen on 127.0.0.1:{port} "
                   f"({os.strerror(errno.EADDRINUSE)})\n")
    with _listening_init("print('started')", port="70o2") as proc:
        out, err = proc.communicate(timeout=30)
    assert (proc.returncode, out) == (1, "")
    assert err == f"webspec-init: refusing to start {sys.executable}: WEBSPEC_INTERNAL_PORT '70o2' is not a port number\n"
    # Bound to what a name resolves to, the socket would be refused by the gateway, which would
    # exit, and the container would restart again and again.
    with _listening_init("print('started')", host="gateway") as proc:
        out, err = proc.communicate(timeout=30)
    assert (proc.returncode, out) == (1, "")
    assert err == (f"webspec-init: refusing to start {sys.executable}: WEBSPEC_HOST 'gateway' is not an IP "
                   "address: the gateway takes the socket this init holds only on a loopback address or on "
                   "exactly the address WEBSPEC_HOST names (DP-8). Set it to an IP address, 0.0.0.0 in the "
                   "compose stack\n")


def test_init_listens_where_the_gateway_would(monkeypatch):
    # C1: WEBSPEC_HOST, empty meaning 127.0.0.1, at WEBSPEC_INTERNAL_PORT, then WEBSPEC_PORT, then 7001.
    init = _load_init(monkeypatch)
    assert init.gateway_address({}) == ("127.0.0.1", 7001)
    assert init.gateway_address({"WEBSPEC_HOST": " ", "WEBSPEC_PORT": "8000"}) == ("127.0.0.1", 8000)
    assert init.gateway_address({"WEBSPEC_HOST": "0.0.0.0", "WEBSPEC_INTERNAL_PORT": "7002",
                                 "WEBSPEC_PORT": "7001"}) == ("0.0.0.0", 7002)
    for port in ("", "70o2", "-1", "65536", "\u0667\u0660\u0660\u0662"):
        with pytest.raises(ValueError, match="is not a port number"):
            init.gateway_address({"WEBSPEC_INTERNAL_PORT": port})


@pytest.mark.parametrize("host, address", [
    ("", "127.0.0.1"),
    (" 0.0.0.0 ", "0.0.0.0"),
    ("::", "::"),
    ("[::]", "::"),  # brackets, as webspec/activation.py reads them
    ("::1", "::1"),
    ("172.18.0.2", "172.18.0.2"),
    ("fd00::2", "fd00::2"),
    ("gateway", None),  # the service's name, which Caddy dials
    ("localhost", None),
    ("[gateway]", None),
    ("0", None),  # getaddrinfo reads these as 0.0.0.0 and 127.0.0.1
    ("127.1", None),
    ("0x7f.0.0.1", None),
    ("010.0.0.1", None),
])
def test_init_holds_only_an_address_the_gateway_takes(monkeypatch, host, address):
    # C1, DP-8: the gateway takes a passed socket only on a loopback address or on exactly the
    # address WEBSPEC_HOST names (webspec/activation.py). The init used to resolve WEBSPEC_HOST
    # and bind what it resolved to, which the gateway then refused: it exited, and the container
    # restarted again and again. So it takes WEBSPEC_HOST as the gateway does, an address or nothing.
    from webspec import activation
    init = _load_init(monkeypatch)
    if address is None:
        with pytest.raises(ValueError, match=re.escape(f"WEBSPEC_HOST {host!r} is not an IP address")):
            init.gateway_address({"WEBSPEC_HOST": host})
        return
    assert init.gateway_address({"WEBSPEC_HOST": host}) == (address, 7001)
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    assert activation._loopback(family, address) or activation._named(host.strip(), address)


def test_init_passes_no_socket_without_listen():
    child = textwrap.dedent("""\
        import os, sys
        try:
            os.fstat(3)
        except OSError:
            sys.exit(any(name.startswith("LISTEN_") for name in os.environ))
        sys.exit(2)
    """)
    assert _supervise(sys.executable, "-c", child).returncode == 0


def test_init_takes_listen_before_the_command(monkeypatch):
    init = _load_init(monkeypatch)
    started = []
    monkeypatch.setattr(init, "make_non_dumpable", lambda: None)
    monkeypatch.setattr(init, "pid1_environment_readable", lambda: False)
    monkeypatch.setattr(init, "start", lambda argv, listen: started.append((argv, listen)) or 0)
    assert init.main(["--listen", "--", "entrypoint.sh", "--listen"]) == 0
    assert init.main(["entrypoint.sh", "--listen"]) == 0
    assert init.main(["--listen"]) == 2
    assert started == [(["entrypoint.sh", "--listen"], True), (["entrypoint.sh", "--listen"], False)]


# ── docker/README.md ──


def _readme():
    return (DOCKER / "README.md").read_text()


def _readme_code_lines():
    """Lines inside the fenced code blocks of docker/README.md."""
    blocks = re.findall(r"^```[a-z]*\n(.*?)^```$", _readme(), re.M | re.S)
    return [line for block in blocks for line in block.splitlines()]


def _readme_commands():
    """Shell commands in docker/README.md's bash blocks, continuation lines joined."""
    blocks = re.findall(r"^```bash\n(.*?)^```$", _readme(), re.M | re.S)
    return [line.strip() for block in blocks for line in re.sub(r"\\\n\s*", " ", block).splitlines()]


def test_readme_never_execs_into_the_gateway():
    # A `docker compose exec` process gets the container's environment, guard key included,
    # and is not non-dumpable, so the stdio MCP servers (same uid) can read it (DP-2).
    lines = _readme_code_lines()
    assert lines
    assert not [line for line in lines if re.search(r"\bexec\b", line) and "gateway" in line]


def test_readme_rotation_keeps_every_segment():
    # AU-4: rotate by renaming. A fixed target name would overwrite the previous segment.
    moves = [line for line in _readme_code_lines() if re.search(r"\bmv\b", line)]
    assert moves
    for line in moves:
        assert " mv -n " in f" {line.strip()} " and "$(date" in line, line


def test_readme_audit_helper_leaves_stdin_alone():
    # `docker run -i` reads all of its standard input. In a script fed to the shell on stdin
    # (`bash -s < steps.sh`), the first `audit` call would swallow the rest of the script, which
    # would then end without an error. Only the heredoc check gets stdin.
    text = _readme()
    audit = re.search(r"^audit\(\) \{\n(.*?)^\}$", text, re.M | re.S).group(1)
    assert "docker run --rm " in audit and not re.search(r"(^|\s)(-[a-z]*i[a-z]*|--interactive)(\s|=|$)", audit)
    audit_stdin = re.search(r"^audit_stdin\(\) \{\n(.*?)^\}$", text, re.M | re.S).group(1)
    assert "docker run --rm -i " in audit_stdin
    calls = [c for c in _readme_commands() if re.match(r"audit(_stdin)? ", c)]
    assert [c for c in calls if c.startswith("audit ")]
    for call in calls:
        assert call.startswith("audit_stdin ") == ("<<" in call), call


def test_readme_demo_server_stays_offline_and_ignores_home():
    # A FastMCP server asks PyPI for updates at startup (DP-3) unless told not to. A Python
    # server without PYTHONNOUSERSITE runs code that other stdio servers plant in HOME. The
    # gateway sets it too, but only when it ignores user site-packages itself: every example
    # sets it, so it is safe whatever the gateway does.
    configs = [json.loads(block) for block in re.findall(r"^```json\n(.*?)^```$", _readme(), re.M | re.S)]
    entries = [entry for config in configs for entry in config.get("mcpServers", {}).values()]
    assert entries
    for entry in entries:
        assert entry["command"] in ("python", "python3")
        assert entry["env"]["FASTMCP_CHECK_FOR_UPDATES"] == "off"
        assert entry["env"]["PYTHONNOUSERSITE"] == "1"


def test_readme_says_what_the_gateway_gives_a_stdio_server():
    # The README promises every stdio server PYTHONNOUSERSITE=1 and / as its working directory.
    # The gateway does both when it ignores user site-packages and its own directory, as
    # `python -I` makes it in the image (test_gateway_python_ignores_user_site_packages).
    from webspec import pool
    text = " ".join(_readme().split())
    assert "it gets `HOME`, `PATH` and `PYTHONNOUSERSITE=1`, plus the `env` its entry sets, starts in `/`" in text
    assert pool.STDIO_ENV_DEFAULTS["PYTHONNOUSERSITE"] == "1"
    assert pool.STDIO_ISOLATED_CWD == "/"


def test_readme_matches_how_the_gateway_notices_an_edit():
    # webspec/config.py, ServiceRegistry: identity, size, modification and change time. A copy
    # that keeps the old modification time applies, and so does a chmod that makes it readable.
    text = " ".join(_readme().split())
    assert "every edit" not in text
    assert "reloads it when it changes" in text
    assert "a replacement that keeps the old modification time (`cp -p`, `install -p`, `rsync -a`, `touch -r`) applies too" in text
    assert "restart the gateway after using one" not in text


def _dockerfile_comment(instruction):
    """The comment right above the Dockerfile's line that starts with instruction, as one line."""
    lines = (DOCKER / "Dockerfile").read_text().splitlines()
    end = next(i for i, line in enumerate(lines) if line.startswith(instruction))
    start = end
    while start and lines[start - 1].startswith("#"):
        start -= 1
    return " ".join(line.lstrip("#").strip() for line in lines[start:end])


def test_readme_says_the_init_holds_the_gateways_port(monkeypatch, capsys):
    # The image passes --listen, so the port stays bound while the gateway shuts down, and after
    # it until every other process in the container is gone, or END_EVERYONE_ELSE_SECONDS for one
    # of another user, which the init cannot kill. Not for the container's whole life: its other
    # processes outlive the init's socket unless the init kills them first.
    init = _load_init(monkeypatch)
    seconds = f"{init.END_EVERYONE_ELSE_SECONDS:g} seconds"
    text = " ".join(_readme().split())
    assert ("The init binds the gateway's port, 7002, before it starts anything, and holds it for the "
            "container's life (`--listen` in the image's `ENTRYPOINT`)") in text
    assert ("When the gateway exits, the init kills every other process in the container, the stdio "
            "servers and whatever they left behind, and lets the port go only when none is left, or "
            f"{seconds} later if one it cannot kill is still running: a process of another user, such "
            "as one that `docker exec -u 0` started, which no stdio server can start. It then logs") in text
    # What it logs then, as the README quotes it.
    clock = itertools.count()  # a second per reading: the deadline comes at once
    monkeypatch.setattr(init, "time", types.SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None))
    monkeypatch.setattr(init, "os", _FakeOS(pid=1, alive=float("inf")))
    init.end_everyone_else("/usr/local/bin/entrypoint.sh")
    logged = capsys.readouterr().err.strip()
    assert logged.startswith(f"webspec-init: other processes are still running {seconds} after ")
    assert f"It then logs `{logged.replace('/usr/local/bin/entrypoint.sh', '...')}`." in text
    assert "The init also holds `[::]:7002`, without listening there" in text
    for stale in ("whole life", "is free inside its container", "take its port"):
        assert stale not in text
    # The gateway never restarts inside its container: when it exits, the container does.
    comment = _dockerfile_comment("ENTRYPOINT")
    assert "holds it for the container's life. When the gateway exits, the init kills every other process" in comment
    assert (f"though it waits only {seconds} for a process of another user, which it cannot kill (a root "
            "`docker exec`)") in comment
    assert "whole life" not in comment and "restarts" not in comment
    # WEBSPEC_HOST, and what the README quotes the init saying about it.
    assert "`WEBSPEC_HOST`, `0.0.0.0` in the stack, must be an IP address" in text
    quoted = re.search(r"`webspec-init: refusing to start \.\.\.: (WEBSPEC_HOST [^`]+)`", text).group(1)
    with pytest.raises(ValueError) as refused:
        init.gateway_address({"WEBSPEC_HOST": "gateway"})
    assert str(refused.value).startswith(quoted)


def test_readme_configures_a_directory():
    commands = _readme_commands()
    assert not [c for c in commands if "WEBSPEC_CONFIG_HOST" in c]
    assert [c for c in commands if c.startswith("WEBSPEC_CONFIG_DIR=")]


def _readme_registry_runs():
    runs = [c for c in _readme_commands() if "webspec_registry" in c]
    assert runs
    return runs


def test_readme_keeps_the_guard_key_from_the_registry():
    # DP-1: the key lives in the gateway's environment, not in an unhardened process of the
    # operator's or the agent's user.
    for run in _readme_registry_runs():
        assert run.startswith("env -u WEBSPEC_GUARD_KEY -u WEBSPEC_GUARD_KEY_FILE "), run
        assert "WEBSPEC_GUARD_KEY=" not in run and "OP_GUARD_KEY_REF" not in run
        assert "WEBSPEC_REGISTRY_HARVEST_GUARDED" not in run


def test_readme_registry_dials_caddy_by_address(compose):
    # By name, localhost resolves to ::1 first, where another local user may listen; the
    # registry refuses such a URL and sets the Host header itself.
    published = {re.fullmatch(r"\[?([^\]]+?)\]?:(\d+):\d+", p).groups() for p in compose["services"]["caddy"]["ports"]}
    assert published == {("127.0.0.1", "7001"), ("::1", "7001")}
    for run in _readme_registry_runs():
        url = urllib.parse.urlsplit(re.search(r"\bWEBSPEC_GATEWAY_URL=(\S+)", run).group(1))
        assert ipaddress.ip_address(url.hostname).is_loopback, run
        assert (url.hostname, str(url.port)) in published, run


def test_readme_queries_the_registry_where_it_listens():
    # The README's curls must reach the port the registry listens on by default, the port the
    # compose block sets and exposes.
    ports = set(re.findall(r"^curl \S*?http://127\.0\.0\.1:(\d+)/", "\n".join(_readme_commands()), re.M))
    text = (DOCKER / "docker-compose.yml").read_text()
    block = re.search(r"^  #     WEBSPEC_REGISTRY_PORT: \"(\d+)\"\n.*?^  #   expose:\n  #     - \"(\d+)\"$", text, re.M | re.S)
    assert block and ports == {block.group(1)} == {block.group(2)}
    from webspec_registry import __main__ as registry
    assert ports == {str(registry.DEFAULT_REGISTRY_PORT)}
    assert f"`WEBSPEC_REGISTRY_PORT`, default `{registry.DEFAULT_REGISTRY_PORT}`" in _readme()


def test_readme_registry_refuses_what_the_readme_says_it_refuses(monkeypatch):
    # The README tells operators that the registry exits on a loopback name, and accepts Caddy's
    # address, which its curls then query.
    from webspec_registry import __main__ as registry
    text = " ".join(_readme().split())
    assert "It refuses, and exits, when the URL names a host that resolves to a loopback address, such as `localhost`" in text
    monkeypatch.setenv("WEBSPEC_GATEWAY_URL", "http://localhost:7001")
    with pytest.raises(ValueError, match="names a loopback address"):
        registry._gateway_url()
    for run in _readme_registry_runs():
        monkeypatch.setenv("WEBSPEC_GATEWAY_URL", re.search(r"\bWEBSPEC_GATEWAY_URL=(\S+)", run).group(1))
        assert registry._gateway_url() == "http://127.0.0.1:7001"


# ── docker/caddy/Caddyfile ──


def _caddyfile():
    """(global options, site block) lines of docker/caddy/Caddyfile, comments stripped."""
    lines = [line.split("#", 1)[0].strip() for line in (DOCKER / "caddy" / "Caddyfile").read_text().splitlines()]
    lines = [line for line in lines if line]
    site = lines.index(":7001 {")
    return lines[:site], lines[site:]


def test_caddy_forwards_only_webspec_hosts():
    # DP-5: loopback names always; destinations under WEBSPEC_DOMAIN only when it is set.
    _, site = _caddyfile()
    assert "@loopback host localhost *.localhost" in site
    assert "host *.{$WEBSPEC_DOMAIN}" in site
    assert 'expression `"{$WEBSPEC_DOMAIN}" != ""`' in site
    assert sum(line.startswith("reverse_proxy gateway:7002") for line in site) == 2
    assert 'respond "Misdirected Request: not a WebSpec host" 421' in site
    # The guard signs the Host header as the client sent it, so it must pass through unchanged.
    assert not any(line.startswith("header_up") for line in site)


def test_caddy_refuses_loopback_hosts_that_came_through_the_tunnel():
    # DP-5: Cloudflare marks what it forwards with Cf-Ray. A loopback Host on such a request was
    # rewritten on the way, and the gateway would serve it as a local request. Only the first
    # matching handle runs, so the refusal comes first.
    _, site = _caddyfile()
    assert [line for line in site if line.startswith("handle")][0] == "handle @tunneled_loopback {"
    matcher = site.index("@tunneled_loopback {")
    assert site[matcher + 1:matcher + 4] == ["host localhost *.localhost", "header Cf-Ray *", "}"]
    handle = site.index("handle @tunneled_loopback {")
    assert site[handle + 1:handle + 5] == [
        'respond "Loopback hosts are not served through the tunnel" 421 {', "close", "}", "}"]


def test_caddy_speaks_http1_only():
    # Without it Caddy also accepts HTTP/2 with prior knowledge (h2c) on :7001, which bare metal
    # (gateway/webspec/caddy.py) does not.
    global_options, _ = _caddyfile()
    servers = global_options.index("servers {")
    assert global_options[servers + 1:servers + 3] == ["protocols h1", "}"]


def test_caddy_keeps_no_access_log_and_redacts_errors():
    # DP-6: GET arguments travel in URLs.
    global_options, site = _caddyfile()
    assert not any(line == "log" or line.startswith("log ") for line in site)
    assert "admin off" in global_options
    assert "request delete" in global_options


@pytest.mark.skipif(not os.environ.get("WEBSPEC_TEST_CADDY"), reason="set WEBSPEC_TEST_CADDY to a caddy binary")
def test_real_caddy_adapts_the_compose_caddyfile(tmp_path):
    binary = os.environ["WEBSPEC_TEST_CADDY"]
    caddyfile = DOCKER / "caddy" / "Caddyfile"
    fmt = subprocess.run([binary, "fmt", "--diff", str(caddyfile)], capture_output=True, text=True)
    assert fmt.returncode == 0, f"not caddy-fmt clean:\n{fmt.stdout}{fmt.stderr}"
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    for domain in ("", "example.com"):
        adapted = subprocess.run([binary, "adapt", "--config", str(caddyfile)], check=True,
                                 capture_output=True, text=True, env={**env, "WEBSPEC_DOMAIN": domain})
        server = json.loads(adapted.stdout)["apps"]["http"]["servers"]["srv0"]
        assert server["listen"] == [":7001"] and server["protocols"] == ["h1"]
        first, *_ = server["routes"][0]["handle"][0]["routes"]
        assert first["match"] == [{"header": {"Cf-Ray": ["*"]}, "host": ["localhost", "*.localhost"]}]
        assert first["handle"][0]["routes"][0]["handle"] == [{
            "body": "Loopback hosts are not served through the tunnel", "close": True,
            "handler": "static_response", "status_code": 421}]
