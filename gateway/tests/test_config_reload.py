"""The gateway notices every change to its config file, not only a new modification time.

ServiceRegistry.check_reload compares the file's identity, size, modification time and change
time. A replacement that keeps the old modification time (cp -p, install -p, rsync -a,
touch -r) must apply, and so must a chmod that makes an unreadable file readable: otherwise
an edit, a tightening one included, is silently lost until the gateway restarts (DP-4).

A change that cannot be loaded, valid JSON that is not a registry included, is rejected as a
whole with one WARNING that names the file and the error: the last good registry stays (an
empty one at startup, which never stops the gateway from starting), and the next change is
read again. Before, such an edit escaped the reload as a KeyError or AttributeError that the
poll logged at DEBUG only, and the next start failed on it.
"""

import asyncio
import json
import logging
import os

import pytest

import webspec.app as appmod
from webspec.config import ServiceRegistry


def write(path, services):
    path.write_text(json.dumps({"mcpServers": {name: {"command": "true"} for name in services}}))


def test_an_unchanged_file_is_not_reloaded(tmp_path):
    config = tmp_path / "claude.json"
    write(config, ["alpha"])
    registry = ServiceRegistry(config)
    assert registry.names() == ["alpha"]
    assert registry.check_reload() is False


def test_a_replacement_that_keeps_the_modification_time_applies(tmp_path):
    config = tmp_path / "claude.json"
    write(config, ["alpha"])
    registry = ServiceRegistry(config)
    before = config.stat()
    replacement = tmp_path / "new.json"
    write(replacement, ["alpha", "beta"])
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))  # as cp -p and rsync -a do
    os.replace(replacement, config)
    assert config.stat().st_mtime_ns == before.st_mtime_ns
    assert registry.check_reload() is True
    assert sorted(registry.names()) == ["alpha", "beta"]


def test_an_in_place_edit_within_the_same_timestamp_applies(tmp_path):
    config = tmp_path / "claude.json"
    write(config, ["alpha"])
    registry = ServiceRegistry(config)
    before = config.stat()
    write(config, ["alpha", "gamma"])  # same inode, new size
    os.utime(config, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert registry.check_reload() is True
    assert sorted(registry.names()) == ["alpha", "gamma"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads whatever the mode says")
def test_a_file_made_readable_is_picked_up_without_a_restart(tmp_path):
    config = tmp_path / "claude.json"
    write(config, ["alpha"])
    config.chmod(0)
    try:
        registry = ServiceRegistry(config)
        assert registry.names() == []
        assert registry.check_reload() is False
        config.chmod(0o644)  # changes the change time only
        assert registry.check_reload() is True
        assert registry.names() == ["alpha"]
    finally:
        config.chmod(0o644)


def test_a_missing_file_is_logged_once_and_its_return_is_picked_up(tmp_path, caplog):
    config = tmp_path / "claude.json"
    write(config, ["alpha"])
    registry = ServiceRegistry(config)
    config.unlink()
    with caplog.at_level(logging.INFO, logger="webspec.config"):
        for _ in range(3):
            assert registry.check_reload() is False
        assert registry.names() == ["alpha"]  # the last good registry stays
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "keeping the last good service registry" in warnings[0].getMessage()
        write(config, ["delta"])
        assert registry.check_reload() is True
        assert registry.names() == ["delta"]
        assert any("can be read again" in r.getMessage() for r in caplog.records)


# ── A file that is valid JSON but not a registry is rejected as a whole ──

GOOD = {"mcpServers": {"alpha": {"command": "true", "level": 0}, "beta": {"command": "true", "level": 0}}}
# The operator's edit: revoke alpha, tighten beta, add tickets. Each variant breaks the file's
# shape in one place, so none of the edit may apply (DP-4: nothing looks applied that is not).
EDIT = {"beta": {"command": "true", "level": 3}}
SHAPES = {
    "http-entry-without-url": ({"mcpServers": {**EDIT, "tickets": {"type": "http", "level": 3}}},
                               "the http entry for 'tickets' has no 'url'"),
    "entry-is-a-string": ({"mcpServers": {**EDIT, "tickets": "https://mcp.example/mcp"}},
                          "the entry for 'tickets' is a string, not an object"),
    "entry-is-null": ({"mcpServers": {**EDIT, "tickets": None}}, "the entry for 'tickets' is null, not an object"),
    "entry-is-an-array": ({"mcpServers": {**EDIT, "tickets": [{"type": "http"}]}},
                          "the entry for 'tickets' is an array, not an object"),
    "mcpservers-is-an-array": ({"mcpServers": [EDIT]}, "mcpServers is an array, not an object"),
    "mcpservers-is-null": ({"mcpServers": None}, "mcpServers is null, not an object"),
    "mcpservers-is-a-string": ({"mcpServers": "beta"}, "mcpServers is a string, not an object"),
    "top-level-is-an-array": ([{"mcpServers": EDIT}], "the top level is an array, not an object"),
    "top-level-is-a-string": ("mcpServers", "the top level is a string, not an object"),
    "top-level-is-a-number": (7, "the top level is a number, not an object"),
    "headers-is-an-array": ({"mcpServers": {**EDIT, "tickets": {"type": "http", "url": "https://mcp.example/mcp",
                                                               "headers": ["Authorization: Bearer x"]}}},
                            "'headers' in the entry for 'tickets' is an array, not an object"),
    "header-value-is-a-number": ({"mcpServers": {**EDIT, "tickets": {"type": "http", "url": "https://mcp.example/mcp",
                                                                    "headers": {"X-Token": 7}}}},
                                 "header 'X-Token' in the entry for 'tickets' is a number, not a string"),
}
# What the operator then fixes it to: the same edit without the broken part.
FIXED = {"mcpServers": {**EDIT, "gamma": {"command": "true"}}}


def config_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "webspec.config" and r.levelno == logging.WARNING]


@pytest.mark.parametrize("shape", SHAPES)
def test_a_file_that_is_not_a_registry_gives_an_empty_registry_at_startup(tmp_path, caplog, shape):
    data, error = SHAPES[shape]
    config = tmp_path / "claude.json"
    config.write_text(json.dumps(data))
    with caplog.at_level(logging.INFO, logger="webspec.config"):
        registry = ServiceRegistry(config)  # what create_app() does: it must not raise
    assert registry.names() == []
    assert config_warnings(caplog) == [
        f"Failed to parse config at {config} (ValueError: {error}) — using empty service registry."]
    config.write_text(json.dumps(FIXED))
    assert registry.check_reload() is True
    assert sorted(registry.names()) == ["beta", "gamma"]


@pytest.mark.parametrize("shape", SHAPES)
def test_a_file_that_is_not_a_registry_keeps_the_last_good_one_until_it_is_fixed(tmp_path, caplog, shape):
    data, error = SHAPES[shape]
    config = tmp_path / "claude.json"
    config.write_text(json.dumps(GOOD))
    registry = ServiceRegistry(config)
    with caplog.at_level(logging.INFO, logger="webspec.config"):
        config.write_text(json.dumps(data))
        assert registry.check_reload() is False  # nothing was reloaded
        assert sorted(registry.names()) == ["alpha", "beta"]  # the revocation did not apply
        assert registry.get("beta").level == 0  # nor did the tightening
        for _ in range(3):  # reported once, not at every poll
            assert registry.check_reload() is False
        assert config_warnings(caplog) == [
            f"Failed to parse config at {config} (ValueError: {error}) — keeping the last good service registry."]
        config.write_text(json.dumps(FIXED))
        assert registry.check_reload() is True
    assert sorted(registry.names()) == ["beta", "gamma"]
    assert registry.get("beta").level == 3


def test_anything_parsing_raises_rejects_the_file(tmp_path, caplog):
    # Not only OSError and ValueError: JSON nested too deeply raises RecursionError.
    config = tmp_path / "claude.json"
    config.write_text(json.dumps(GOOD))
    registry = ServiceRegistry(config)
    config.write_text('{"mcpServers": ' + "[" * 100_000 + "]" * 100_000 + "}")
    with caplog.at_level(logging.INFO, logger="webspec.config"):
        assert registry.check_reload() is False
        assert ServiceRegistry(config).names() == []  # and a start does not fail on it
    assert sorted(registry.names()) == ["alpha", "beta"]
    warnings = config_warnings(caplog)
    assert len(warnings) == 2 and all(f"Failed to parse config at {config} (RecursionError: " in m for m in warnings)


class RecordingPool:
    def __init__(self):
        self.removed: list[str] = []

    async def remove_service(self, name):
        self.removed.append(name)


def run_reload_loop(monkeypatch, until, polls_after=5, timeout=10.0):
    """Run the gateway's own poll (app._config_reload_loop) until ``until()``, then a few polls more."""
    monkeypatch.setattr(appmod, "CONFIG_POLL_INTERVAL", 0.01)

    async def scenario():
        task = asyncio.create_task(appmod._config_reload_loop())
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while not until():
                assert loop.time() < deadline, "the reload loop never got there"
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.01 * polls_after)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_the_poll_reports_a_rejected_edit_once_and_applies_its_fix(tmp_path, caplog, monkeypatch):
    config = tmp_path / "claude.json"
    config.write_text(json.dumps(GOOD))
    registry, pool = ServiceRegistry(config), RecordingPool()
    monkeypatch.setattr(appmod, "registry", registry)
    monkeypatch.setattr(appmod, "pool", pool)
    caplog.set_level(logging.INFO, logger="webspec")
    config.write_text(json.dumps(SHAPES["http-entry-without-url"][0]))
    run_reload_loop(monkeypatch, until=lambda: config_warnings(caplog))
    assert config_warnings(caplog) == [f"Failed to parse config at {config} (ValueError: the http entry for "
                                       "'tickets' has no 'url') — keeping the last good service registry."]
    assert sorted(registry.names()) == ["alpha", "beta"] and pool.removed == []
    assert not [r for r in caplog.records if "Config reload" in r.getMessage()]  # nothing looks applied

    config.write_text(json.dumps(FIXED))
    run_reload_loop(monkeypatch, until=lambda: "gamma" in registry.names(), polls_after=0)
    assert pool.removed == ["alpha"]  # the revocation applies with the fix
    assert registry.get("beta").level == 3
    assert "Config reload: removing service alpha" in caplog.messages


def test_the_poll_logs_other_failures_as_warnings(tmp_path, caplog, monkeypatch):
    # The default log level is info: a failure logged at DEBUG was never seen.
    config = tmp_path / "claude.json"
    config.write_text(json.dumps(GOOD))
    registry = ServiceRegistry(config)

    def broken():
        raise RuntimeError("stat failed oddly")

    monkeypatch.setattr(registry, "check_reload", broken)
    monkeypatch.setattr(appmod, "registry", registry)
    monkeypatch.setattr(appmod, "pool", RecordingPool())
    caplog.set_level(logging.INFO, logger="webspec")
    failures = lambda: [r for r in caplog.records if r.getMessage() == "Config reload check failed"]  # noqa: E731
    run_reload_loop(monkeypatch, until=failures, polls_after=0)
    record = failures()[0]
    assert record.levelno == logging.WARNING and record.name == "webspec"
    assert record.exc_info and record.exc_info[0] is RuntimeError
