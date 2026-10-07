"""Without a usable guard key the gateway fails closed (GD-5), audits it (AU-1), and says so.

A request that needs the key gets a plain-text 500 and an audit entry with outcome ``error``
and reason ``guard_key_unavailable``; the log gets one line per refusal instead of a
traceback. At startup the gateway warns once when no usable key is configured. The real app
over the fake registry and pool of tests/_gateway.py.
"""

import hashlib
import json
import logging

import pytest

import webspec.app as appmod
import webspec.config as config
from webspec import audit
from webspec.guard import compute_guard_hmac
from tests._gateway import GUARD_KEY_HEX, FakePool, entry, make_client, request, tool

H = "svc.localhost"
TOOLS = [
    tool("read_note", read_only=True, open_world=False),
    tool("send_note", read_only=False, destructive=False, idempotent=False, open_world=False),
    tool("read_secret", read_only=True, open_world=False, tier="sensitive"),
]
PLAIN_500 = (500, "text/plain; charset=utf-8", "Internal Server Error")


def _keyless(monkeypatch):
    for var in ("WEBSPEC_GUARD_KEY", "WEBSPEC_GUARD_KEY_FILE", "WEBSPEC_GUARD_KEY_DEV_EPHEMERAL"):
        monkeypatch.delenv(var, raising=False)


def _audit_lines(tmp_path) -> list[dict]:
    path = tmp_path / "audit.jsonl"  # conftest points WEBSPEC_AUDIT_LOG here
    if not path.exists():
        return []
    assert audit.verify_chain(path) is None
    return [json.loads(line) for line in path.read_text().splitlines()]


def _fields(line: dict, *names: str) -> dict:
    return {name: line[name] for name in names}


def _error_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_an_unsafe_request_without_the_key_is_an_audited_plain_500(monkeypatch, tmp_path, caplog):
    client, pool = make_client(monkeypatch, [entry(level=0)], TOOLS)
    _keyless(monkeypatch)
    with caplog.at_level(logging.INFO, logger="webspec"):
        r = request(client, "POST", H, "/send_note", body=b'{"to":"ana"}',
                    headers={"X-Gimme-Definer": "SEND", "Content-Type": "application/json"})
    assert (r.status_code, r.headers["content-type"], r.text) == PLAIN_500
    assert pool.calls == []
    [line] = _audit_lines(tmp_path)
    assert _fields(line, "service", "method", "path", "tool", "level", "outcome", "status", "reason") == {
        "service": "svc", "method": "POST", "path": "/send_note", "tool": "send_note", "level": 0,
        "outcome": "error", "status": 500, "reason": "guard_key_unavailable"}
    # One line, no traceback, naming what is missing.
    [record] = _error_records(caplog)
    assert record.exc_info is None and "\n" not in record.getMessage()
    assert "Refused POST on svc with 500" in record.getMessage()
    assert "WEBSPEC_GUARD_KEY is not set" in record.getMessage()


def test_a_safe_level0_read_still_needs_no_key(monkeypatch, tmp_path):
    client, pool = make_client(monkeypatch, [entry(level=0)], TOOLS)
    _keyless(monkeypatch)
    r = request(client, "GET", H, "/read_note", query="id=1")
    assert r.status_code == 200, r.text
    assert [line["outcome"] for line in _audit_lines(tmp_path)] == ["invoked"]


@pytest.mark.parametrize("method, path", [("GET", "/__nonce"), ("GET", "/read_note"), ("POST", "/send_note"),
                                          ("OPTIONS", "/"), ("HEAD", "/read_note")])
def test_a_guarded_destination_without_the_key(monkeypatch, tmp_path, caplog, method, path):
    client, pool = make_client(monkeypatch, [entry(level=1)], TOOLS)
    _keyless(monkeypatch)
    with caplog.at_level(logging.INFO, logger="webspec"):
        r = client.request(method, path, headers={"Host": H, "X-WebSpec-Guard": "00000000",
                                                  "X-WebSpec-Nonce": "n", "X-Gimme-Definer": "SEND"})
    assert (r.status_code, r.headers["content-type"]) == PLAIN_500[:2]
    if method != "HEAD":
        assert r.text == "Internal Server Error"
    assert pool.calls == []
    # Refused before the tool is resolved, so the entry has no tool, like a failed guard's.
    [line] = _audit_lines(tmp_path)
    assert _fields(line, "service", "method", "path", "tool", "outcome", "status", "reason") == {
        "service": "svc", "method": method, "path": path, "tool": None,
        "outcome": "error", "status": 500, "reason": "guard_key_unavailable"}
    [record] = _error_records(caplog)
    assert record.exc_info is None and f"Refused {method} on svc" in record.getMessage()


class _KeyVanishesPool(FakePool):
    """Removes the key file when the tool list is read, after the guard check has used the key."""

    def __init__(self, tools, key_file):
        super().__init__(tools)
        self.key_file = key_file

    async def list_tools(self, name):
        self.key_file.unlink(missing_ok=True)
        return self.tools


@pytest.mark.parametrize("level, method, path, headers", [
    (1, "POST", "/send_note", {"X-Gimme-Definer": "SEND"}),  # the definer check needs the key
    (3, "GET", "/read_secret", {}),  # so does the clearance check
], ids=["definer", "clearance"])
def test_a_key_lost_after_the_guard_check_is_refused_with_the_tool_resolved(monkeypatch, tmp_path, caplog,
                                                                             level, method, path, headers):
    key_file = tmp_path / "guard.key"
    key_file.write_text(GUARD_KEY_HEX + "\n")
    client, _ = make_client(monkeypatch, [entry(level=level)], TOOLS)
    _keyless(monkeypatch)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(key_file))
    pool = appmod.pool = _KeyVanishesPool(TOOLS, key_file)
    with caplog.at_level(logging.INFO, logger="webspec"):
        r = request(client, method, H, path, guarded=True, headers=headers)
    assert (r.status_code, r.headers["content-type"], r.text) == PLAIN_500
    assert pool.calls == []
    [line] = _audit_lines(tmp_path)
    assert _fields(line, "path", "tool", "level", "outcome", "status", "reason") == {
        "path": path, "tool": path[1:], "level": level,
        "outcome": "error", "status": 500, "reason": "guard_key_unavailable"}
    [record] = _error_records(caplog)
    assert record.exc_info is None and str(key_file) in record.getMessage()
    assert GUARD_KEY_HEX not in caplog.text


# ── At startup ──


def _startup_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == "webspec"]


def test_startup_warns_once_when_no_key_is_configured(monkeypatch, caplog):
    _keyless(monkeypatch)
    with caplog.at_level(logging.INFO, logger="webspec"):
        appmod.create_app()
    [message] = _startup_warnings(caplog)
    assert "every guarded request and every unsafe request is refused with a plain-text 500" in message
    assert "WEBSPEC_GUARD_KEY is not set" in message and "WEBSPEC_GUARD_KEY_FILE" in message


def test_startup_names_a_broken_key_file_and_never_its_content(monkeypatch, tmp_path, caplog):
    key_file = tmp_path / "guard.key"
    key_file.write_text("tiny-secret\n")  # shorter than 16 characters
    _keyless(monkeypatch)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(key_file))
    with caplog.at_level(logging.INFO, logger="webspec"):
        appmod.create_app()
    [message] = _startup_warnings(caplog)
    assert str(key_file) in message and "fewer than 16 characters" in message
    assert "tiny-secret" not in caplog.text


@pytest.mark.parametrize("source", ["env", "file", "dev"])
def test_startup_is_quiet_with_a_key(monkeypatch, tmp_path, caplog, source):
    _keyless(monkeypatch)
    monkeypatch.setattr(config, "_dev_ephemeral_key", None)
    if source == "env":
        monkeypatch.setenv("WEBSPEC_GUARD_KEY", GUARD_KEY_HEX)
    elif source == "file":
        (tmp_path / "guard.key").write_text(GUARD_KEY_HEX + "\n")
        monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(tmp_path / "guard.key"))
    else:
        monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    with caplog.at_level(logging.INFO, logger="webspec"):
        appmod.create_app()
    assert _startup_warnings(caplog) == []
    assert GUARD_KEY_HEX not in caplog.text


# ── A key that is set but not UTF-8 ──

NOT_UTF8 = "secret-key\udcffmaterial"  # how os.environ holds the byte 0xFF


def test_startup_survives_a_key_that_is_not_utf8_and_says_why(monkeypatch, caplog):
    # It used to raise UnicodeEncodeError out of create_app(): no gateway at all, and under
    # Restart=always a restart loop whose tracebacks named a key byte and its position.
    _keyless(monkeypatch)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", NOT_UTF8)
    with caplog.at_level(logging.INFO, logger="webspec"):
        appmod.create_app()
    [message] = _startup_warnings(caplog)
    assert "WEBSPEC_GUARD_KEY is not valid UTF-8" in message
    assert "secret" not in caplog.text and "material" not in caplog.text


def test_a_request_needing_a_key_that_is_not_utf8_is_an_audited_plain_500(monkeypatch, tmp_path, caplog):
    client, pool = make_client(monkeypatch, [entry(level=1)], TOOLS)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", NOT_UTF8)
    with caplog.at_level(logging.INFO, logger="webspec"):
        r = client.get("/__nonce", headers={"Host": H, "X-WebSpec-Guard": "00000000"})
    assert (r.status_code, r.headers["content-type"], r.text) == PLAIN_500
    [line] = _audit_lines(tmp_path)
    assert _fields(line, "outcome", "status", "reason") == {
        "outcome": "error", "status": 500, "reason": "guard_key_unavailable"}
    [record] = _error_records(caplog)
    assert record.exc_info is None and "WEBSPEC_GUARD_KEY is not valid UTF-8" in record.getMessage()
    assert "secret" not in caplog.text and "material" not in caplog.text


# ── A key of invisible characters only ──

ZERO_WIDTH_SPACE = "\u200b"  # all that is left of a key copied from an empty cell of a web page


def test_a_key_of_invisible_characters_only_does_not_admit_its_public_hash(monkeypatch, tmp_path, caplog):
    # It used to derive sha256(U+200B), so anyone could sign a guard tag and get a nonce.
    client, pool = make_client(monkeypatch, [entry(level=1)], TOOLS)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", ZERO_WIDTH_SPACE)
    public_key = hashlib.sha256(ZERO_WIDTH_SPACE.encode()).digest()
    mac = compute_guard_hmac(public_key, "GET", H, "/__nonce", "", b"")
    with caplog.at_level(logging.INFO, logger="webspec"):
        r = client.get("/__nonce", headers={"Host": H, "X-WebSpec-Guard": mac})
    assert (r.status_code, r.headers["content-type"], r.text) == PLAIN_500
    [line] = _audit_lines(tmp_path)
    assert _fields(line, "outcome", "status", "reason") == {
        "outcome": "error", "status": 500, "reason": "guard_key_unavailable"}
    [record] = _error_records(caplog)
    assert "WEBSPEC_GUARD_KEY is not set (it holds only whitespace or invisible characters)" in record.getMessage()


def test_startup_warns_about_a_key_of_invisible_characters_only(monkeypatch, caplog):
    _keyless(monkeypatch)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", ZERO_WIDTH_SPACE)
    with caplog.at_level(logging.INFO, logger="webspec"):
        appmod.create_app()
    [message] = _startup_warnings(caplog)
    assert "WEBSPEC_GUARD_KEY is not set (it holds only whitespace or invisible characters)" in message
