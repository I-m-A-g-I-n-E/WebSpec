"""${VAR} expansion in a stdio server's env, the same as in an http server's headers.

The config file names a secret; the gateway's environment supplies it (on Linux, systemd
loads /etc/webspec/gateway.env). Real config files and, for the last test, a real stdio
server behind the real gateway: no mocks.
"""

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import webspec.app as appmod
from webspec.config import parse_claude_config

GATEWAY = Path(__file__).resolve().parents[1]
EXAMPLE = GATEWAY / "deploy" / "config.example.json"
ENV_SERVER = Path(__file__).parent / "fixtures" / "env_server.py"


def _config(tmp_path, servers: dict, **top) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**top, "mcpServers": servers}))
    return path


def _stdio(env) -> dict:
    return {"command": "/opt/webspec/venv/bin/python", "args": ["server.py"], "env": env}


def test_stdio_env_values_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "tok-123")
    monkeypatch.setenv("REGION", "eu")
    path = _config(tmp_path, {"svc": _stdio({"SOME_TOKEN": "${SOME_TOKEN}", "URL": "https://${REGION}.example/${REGION}",
                                             "PLAIN": "as-is"})})
    assert parse_claude_config(path)["svc"].env == {"SOME_TOKEN": "tok-123", "URL": "https://eu.example/eu",
                                                    "PLAIN": "as-is"}


def test_unresolved_stdio_env_values_are_kept_literally(tmp_path, monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    path = _config(tmp_path, {"svc": _stdio({"TOKEN": "${NOT_SET_ANYWHERE}"})})
    assert parse_claude_config(path)["svc"].env == {"TOKEN": "${NOT_SET_ANYWHERE}"}


def test_stdio_env_is_expanded_on_every_reload(tmp_path, monkeypatch):
    path = _config(tmp_path, {"svc": _stdio({"TOKEN": "${ROTATING}"})})
    monkeypatch.setenv("ROTATING", "one")
    assert parse_claude_config(path)["svc"].env == {"TOKEN": "one"}
    monkeypatch.setenv("ROTATING", "two")
    assert parse_claude_config(path)["svc"].env == {"TOKEN": "two"}


@pytest.mark.parametrize("raw, parsed", [(None, {}), ({}, {}), ({"N": 3}, {"N": 3}), (["x"], ["x"])])
def test_absent_or_malformed_stdio_env_parses_as_before(tmp_path, raw, parsed):
    # Shallow validation, as before: only strings are expanded, and a malformed env is
    # passed through to fail when that server is spawned, not when the config loads.
    path = _config(tmp_path, {"svc": _stdio(raw)})
    assert parse_claude_config(path)["svc"].env == parsed


def test_stdio_without_env(tmp_path):
    path = _config(tmp_path, {"svc": {"command": "server"}})
    assert parse_claude_config(path)["svc"].env == {}


def test_unknown_top_level_keys_are_ignored(tmp_path):
    path = _config(tmp_path, {"svc": {"command": "server"}}, _comment="notes for the operator", other={"x": 1})
    assert list(parse_claude_config(path)) == ["svc"]


def test_the_deployment_example_parses(monkeypatch):
    # gateway/deploy/config.example.json is the entry format the installers put beside the live
    # config, as /etc/webspec/config.example.json; the gateway never reads it.
    monkeypatch.setenv("SOME_TOKEN", "tok-from-gateway-env")
    monkeypatch.setenv("TICKETS_TOKEN", "tickets-from-gateway-env")
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert set(data) == {"_comment", "mcpServers"}
    registry = parse_claude_config(EXAMPLE)
    assert set(registry) == {"notes", "tickets"}

    notes = registry["notes"]
    assert notes.transport_type == "stdio" and notes.command.startswith("/")
    assert notes.env == {"SOME_TOKEN": "tok-from-gateway-env"}
    assert notes.level == 4 and notes.guard is True
    assert notes.tools == {"read_note": {"read_only": True, "open_world": False}}

    tickets = registry["tickets"]
    assert tickets.transport_type == "http" and tickets.url.startswith("https://")
    assert tickets.headers == {"Authorization": "Bearer tickets-from-gateway-env"}
    assert tickets.level == 3

    # The file itself holds references only, never secret values.
    raw = data["mcpServers"]
    assert raw["notes"]["env"] == {"SOME_TOKEN": "${SOME_TOKEN}"}
    assert raw["tickets"]["headers"] == {"Authorization": "Bearer ${TICKETS_TOKEN}"}


GUARD_KEY = "44" * 32


@pytest.mark.parametrize("name", ["WEBSPEC_GUARD_KEY", "WEBSPEC_GUARD_KEY_FILE", "webspec_guard_key"])
def test_the_guard_key_is_never_expanded_into_a_server(tmp_path, monkeypatch, caplog, name):
    """GD-5: no config can hand the guard key, or where it lives, to an MCP server. The
    reference stays literal (like an unresolved one), the rest still expands, and a warning
    names the service and the variable, never the value."""
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", GUARD_KEY)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", "/etc/webspec/guard.key")
    monkeypatch.setenv("TICKETS_TOKEN", "tickets-tok")
    ref = "${" + name + "}"
    path = _config(tmp_path, {
        "stdio": _stdio({"KEY": ref, "BOTH": "${TICKETS_TOKEN}:" + ref}),
        "web": {"type": "http", "url": "https://mcp.example/mcp",
                "headers": {"Authorization": "Bearer " + ref, "X-Token": "${TICKETS_TOKEN}"}},
    })
    with caplog.at_level("WARNING", logger="webspec.config"):
        registry = parse_claude_config(path)
    assert registry["stdio"].env == {"KEY": ref, "BOTH": "tickets-tok:" + ref}
    assert registry["web"].headers == {"Authorization": "Bearer " + ref, "X-Token": "tickets-tok"}
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 3
    assert any(m.startswith("Service stdio: " + ref) for m in warnings)
    assert any(m.startswith("Service web: " + ref) for m in warnings)
    assert not any(GUARD_KEY in m or "/etc/webspec/guard.key" in m for m in caplog.messages)


def test_stdio_server_gets_its_expanded_env_and_not_the_guard_key(tmp_path, monkeypatch):
    """Real gateway, real stdio server: it receives the variables its entry names, expanded,
    and none of the gateway's own environment (no WEBSPEC_GUARD_KEY, even when asked for)."""
    path = _config(tmp_path, {"probe": {"command": sys.executable, "args": [str(ENV_SERVER)],
                                        "env": {"SOME_TOKEN": "${SOME_TOKEN}",
                                                "ASKED_FOR_THE_KEY": "${WEBSPEC_GUARD_KEY}"}}})
    monkeypatch.setenv("WEBSPEC_CONFIG", str(path))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "33" * 32)
    monkeypatch.setenv("SOME_TOKEN", "tok-456")
    monkeypatch.setenv("UNREFERENCED_SECRET", "must-not-leak")
    monkeypatch.delenv("WEBSPEC_DOMAIN", raising=False)
    host = {"Host": "probe.localhost"}
    with TestClient(appmod.create_app()) as client:
        def getenv(name):
            r = client.get(f"/getenv?name={name}", headers=host)
            assert r.status_code == 200, r.text
            return r.json()["result"]

        assert getenv("SOME_TOKEN") == "tok-456"
        assert getenv("WEBSPEC_GUARD_KEY") == "<unset>"
        assert getenv("UNREFERENCED_SECRET") == "<unset>"
        assert getenv("ASKED_FOR_THE_KEY") == "${WEBSPEC_GUARD_KEY}"  # GD-5: never expanded
