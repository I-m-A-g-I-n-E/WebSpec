"""WEBSPEC_REQUIRE_GUARD_KEY=1: no start without a usable key in WEBSPEC_GUARD_KEY itself (GD-5).

The Linux unit sets it, and clears WEBSPEC_GUARD_KEY_FILE and WEBSPEC_GUARD_KEY_DEV_EPHEMERAL.
A shell check such as the unit's ExecStartPre= sees a key in any byte that is not ASCII
whitespace, so a value of invisible characters only (U+200B, U+00A0) used to start a gateway
that refused every request needing the key, while systemd and the installer reported it
running. Now webspec/__main__.py applies config's own rule (env_guard_key) before it serves:
it logs one line, never the value, and exits with STARTUP_FAILURE; the key file and the dev
key are not tried. Without the variable, nothing changes: such a gateway warns and starts.
"""

import logging
import re
import socket
import subprocess
import sys

import pytest

import webspec.__main__ as gateway_main
from webspec import config
from tests.test_activation import GATEWAY, activated, child_env, free_port, listening_socket, serves, stop

KEY_FILE_HEX = "05" * 32

# What WEBSPEC_GUARD_KEY may hold that is no key by config's rules.
UNUSABLE = {
    "unset": None,
    "empty": "",
    "spaces": "   ",
    "zero-width-space": "\u200b",
    "no-break-space": "\u00a0",
    "bom": "\ufeff",
    "word-joiner": "\u2060",
    "ideographic-space": "\u3000",
    "mixed": "\u200b\u00a0 \u2060",
    "not-utf8": "s3cret\udcff",  # bytes that are not UTF-8 reach os.environ as lone surrogates
}


@pytest.fixture
def startup(monkeypatch):
    """main() with nothing real behind it: records what it hardens, takes, builds and serves."""
    calls: list[str] = []
    monkeypatch.setattr(gateway_main, "harden_process", lambda: calls.append("harden"))
    monkeypatch.setattr(gateway_main.activation, "inherited_socket", lambda: calls.append("socket"))
    monkeypatch.setattr(gateway_main, "create_app", lambda: calls.append("app") or object())
    monkeypatch.setattr(gateway_main.uvicorn, "run", lambda app, **kw: calls.append("serve"))
    return calls


@pytest.fixture
def fallbacks(monkeypatch, tmp_path):
    """The two other key sources, both usable: under WEBSPEC_REQUIRE_GUARD_KEY neither may count."""
    key_file = tmp_path / "guard.key"
    key_file.write_text(KEY_FILE_HEX + "\n")
    key_file.chmod(0o400)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(key_file))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    monkeypatch.setattr(config, "_dev_ephemeral_key", None)


def set_key(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("WEBSPEC_GUARD_KEY", raising=False)
    else:
        monkeypatch.setenv("WEBSPEC_GUARD_KEY", value)


@pytest.mark.parametrize("value", UNUSABLE.values(), ids=UNUSABLE.keys())
def test_a_required_key_that_is_not_usable_stops_the_start(monkeypatch, caplog, startup, fallbacks, value):
    monkeypatch.setenv("WEBSPEC_REQUIRE_GUARD_KEY", "1")
    set_key(monkeypatch, value)
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exc:
        gateway_main.main()
    assert exc.value.code == gateway_main.STARTUP_FAILURE
    assert startup == []  # the first thing it does: nothing taken, built or served
    assert config._dev_ephemeral_key is None  # no dev key minted
    problems = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(problems) == 1, caplog.text  # one line
    line = problems[0].getMessage()
    reason = ("WEBSPEC_GUARD_KEY is not valid UTF-8" if value and "\udcff" in value else
              "WEBSPEC_GUARD_KEY is not set (it holds only whitespace or invisible characters)" if value else
              "WEBSPEC_GUARD_KEY is not set")
    assert line == (f"No usable guard key, not starting: {reason}. WEBSPEC_REQUIRE_GUARD_KEY is set, so the key "
                    "must be in WEBSPEC_GUARD_KEY itself, not in a key file or an ephemeral key (GD-5)")
    assert "s3cret" not in caplog.text and KEY_FILE_HEX not in caplog.text


@pytest.mark.parametrize("value", ["short", "\u200bshort", "01" * 32])
def test_a_required_key_that_is_usable_starts(monkeypatch, startup, fallbacks, value):
    monkeypatch.setenv("WEBSPEC_REQUIRE_GUARD_KEY", "1")
    set_key(monkeypatch, value)
    gateway_main.main()
    assert startup == ["harden", "socket", "app", "serve"]


@pytest.mark.parametrize("value", [None, "\u200b", "\u00a0"], ids=["unset", "zero-width-space", "no-break-space"])
@pytest.mark.parametrize("require", [None, "", "0", " 0 "])
def test_without_the_requirement_a_gateway_without_a_key_still_starts(monkeypatch, startup, require, value):
    # As before: it warns (app._report_guard_key) and refuses each request that needs the key.
    if require is None:
        monkeypatch.delenv("WEBSPEC_REQUIRE_GUARD_KEY", raising=False)
    else:
        monkeypatch.setenv("WEBSPEC_REQUIRE_GUARD_KEY", require)
    set_key(monkeypatch, value)
    gateway_main.main()
    assert startup == ["harden", "socket", "app", "serve"]


@pytest.mark.parametrize("require, required", [
    (None, False), ("", False), ("0", False), (" 0 ", False), ("  ", False),
    ("1", True), (" 1 ", True), ("yes", True), ("true", True),  # it only tightens: a typo cannot turn it off
])
def test_which_values_require_the_key(monkeypatch, require, required):
    if require is None:
        monkeypatch.delenv("WEBSPEC_REQUIRE_GUARD_KEY", raising=False)
    else:
        monkeypatch.setenv("WEBSPEC_REQUIRE_GUARD_KEY", require)
    assert gateway_main.guard_key_required() is required


# ── The real gateway process ──────────────────────────────────────────────────


def gateway_env(tmp_path, port: int, key: str | None, **extra: str) -> dict[str, str]:
    config_file = tmp_path / "config.json"
    config_file.write_text('{"mcpServers": {}}')
    key_file = tmp_path / "guard.key"
    key_file.write_text(KEY_FILE_HEX + "\n")
    env = child_env(WEBSPEC_CONFIG=str(config_file), WEBSPEC_AUDIT_LOG="", WEBSPEC_HOST="127.0.0.1",
                    WEBSPEC_PORT="7001", WEBSPEC_INTERNAL_PORT=str(port), WEBSPEC_REQUIRE_GUARD_KEY="1",
                    # Both usable, and neither may count.
                    WEBSPEC_GUARD_KEY_FILE=str(key_file), WEBSPEC_GUARD_KEY_DEV_EPHEMERAL="1", **extra)
    if key is not None:
        env["WEBSPEC_GUARD_KEY"] = key
    return env


def refused(proc: subprocess.Popen) -> str:
    try:
        _, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()  # it started after all: never leave it running
        raise AssertionError(f"the gateway did not exit: {proc.communicate()[1]}") from None
    assert proc.returncode == gateway_main.STARTUP_FAILURE, err
    lines = [ln for ln in err.splitlines() if "No usable guard key" in ln]
    assert len(lines) == 1 and "not starting" in lines[0], err
    assert [ln for ln in err.splitlines() if re.search(r" \[webspec[.\w]*\] ", ln)] == lines, err  # its only line
    for served in ("Uvicorn running on", "Application startup", "Listening on", "Registered services"):
        assert served not in err, err
    assert "s3cret" not in err and KEY_FILE_HEX not in err and "insecure in-memory guard key" not in err
    return lines[0]


@pytest.mark.parametrize("key, reason", [
    ("\u200b", "it holds only whitespace or invisible characters"),
    ("\u00a0", "it holds only whitespace or invisible characters"),
    ("\u200b\u00a0\ufeff", "it holds only whitespace or invisible characters"),
    ("s3cret\udcff\udcfe", "WEBSPEC_GUARD_KEY is not valid UTF-8"),  # the bytes ff fe in the environment
], ids=["zero-width-space", "no-break-space", "mixed", "not-utf8"])
def test_the_gateway_exits_before_it_binds(tmp_path, key, reason):
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "webspec"], cwd=GATEWAY, env=gateway_env(tmp_path, port, key),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert reason in refused(proc)


@pytest.mark.parametrize("key", ["\u200b", "\u00a0"], ids=["zero-width-space", "no-break-space"])
def test_the_gateway_exits_before_it_takes_the_socket_systemd_passes(tmp_path, key):
    """As under the Linux unit: the start fails visibly, and the port stays with its holder."""
    with listening_socket() as sock:
        port = sock.getsockname()[1]
        refused(activated(sock.fileno(), ["-m", "webspec"], env=gateway_env(tmp_path, port, key)))
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass  # still listening: a connection waits for the next gateway


def test_the_gateway_serves_with_a_usable_key(tmp_path):
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "webspec"], cwd=GATEWAY,
                            env=gateway_env(tmp_path, port, "\u200bpassphrase"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert serves(port, proc) == 200
    finally:
        err = stop(proc)
    assert "No usable guard key" not in err


def test_without_the_requirement_the_gateway_still_starts_without_a_key(tmp_path):
    port = free_port()
    env = gateway_env(tmp_path, port, "\u200b")
    for name in ("WEBSPEC_REQUIRE_GUARD_KEY", "WEBSPEC_GUARD_KEY_FILE", "WEBSPEC_GUARD_KEY_DEV_EPHEMERAL"):
        env.pop(name)
    proc = subprocess.Popen([sys.executable, "-m", "webspec"], cwd=GATEWAY, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert serves(port, proc) == 200
    finally:
        err = stop(proc)
    assert "No usable guard key, so every guarded request and every unsafe request is refused" in err
    assert "not starting" not in err
