"""WebSpec v2, the rulings that test_v2_http.py and test_route_rule.py do not already cover.

The idempotency record keeps the resolved tool (ID-2), level 0 needs no guard key (GD-5), one
definer field line on every method (DF-2), the CORS lists (DP-8), no-store on nonces (GD-4),
closed keys in discovery (PA-9, DS-1), the AU-5 record, keys compared trimmed (PA-6), and
bindings that fail closed (PA-8).
"""

from __future__ import annotations

import json
import logging

import pytest

from webspec import handlers, routes
from webspec.config import parse_claude_config
from webspec.guard import compute_guard_hmac
from webspec.methods import ToolContract

from tests._gateway import KEY, entry, make_client, tool
from tests.test_v2_http import H, answer, audit_records, notes_tools, send


@pytest.fixture(autouse=True)
def _fresh_route_maps(monkeypatch):
    monkeypatch.setattr(handlers, "route_maps", {})


@pytest.fixture
def notes(monkeypatch):
    return make_client(monkeypatch, [entry(level=0)], notes_tools())


def mail_tools():
    return [tool("send_email", read_only=False, destructive=False, open_world=True),
            tool("create_email", read_only=False, destructive=False, open_world=False)]


def test_the_same_idempotency_key_on_another_post_action_is_422(monkeypatch):
    client, pool = make_client(monkeypatch, [entry(level=0)], mail_tools())
    body = b'{"to":"x"}'
    r = send(client, "POST", "/email", body=body, headers={"WebSpec-Definer": "SEND", "Idempotency-Key": "k-1"})
    assert r.status_code == 200
    # Same method, path, query and body, so the same fingerprint; but CREATE resolves to another tool.
    r = send(client, "POST", "/email", body=body, headers={"WebSpec-Definer": "CREATE", "Idempotency-Key": "k-1"})
    assert answer(r) == (422, "idempotency_key_reused")
    assert [c[1] for c in pool.calls] == ["send_email"]


def test_level_0_serves_unsafe_requests_without_a_guard_key(monkeypatch, notes):
    client, pool = notes
    monkeypatch.delenv("WEBSPEC_GUARD_KEY")
    monkeypatch.delenv("WEBSPEC_GUARD_KEY_FILE", raising=False)
    assert send(client, "POST", "/email", body=b'{"to":"x"}', headers={"WebSpec-Definer": "SEND"}).status_code == 200
    assert send(client, "DELETE", "/note", query="id=welcome", headers={"WebSpec-Definer": "REMOVE"}).status_code == 200
    assert [c[1] for c in pool.calls] == ["send_email", "delete_note"]


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_two_definer_lines_are_refused_on_every_method_before_the_guard(monkeypatch, method):
    client, pool = make_client(monkeypatch, [entry(level=1)], notes_tools())
    r = client.request(method, "/note?id=welcome", headers=[
        ("Host", H), ("WebSpec-Definer", "SEND"), ("WebSpec-Definer", "SEND")])
    assert r.status_code == 400 and r.headers["cache-control"] == "no-store"  # not 401: before the guard
    if method != "HEAD":
        assert answer(r) == (400, "unknown_definer")
    assert pool.calls == []


def test_an_allowed_origin_can_read_the_taint_mark(monkeypatch):
    monkeypatch.setenv("WEBSPEC_CORS_ORIGINS", "https://app.example")
    client, _ = make_client(monkeypatch, [entry(level=0)], notes_tools())
    r = send(client, "POST", "/email", body=b'{"to":"x"}',
             headers={"WebSpec-Definer": "SEND", "Origin": "https://app.example"})
    assert r.status_code == 200 and r.headers["webspec-taint"] == "open-world"
    exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
    assert {"allow", "link", "webspec-level", "webspec-tier", "webspec-taint", "webspec-status",
            "webspec-tool-count", "webspec-service", "idempotent-replayed"} <= exposed


def test_a_nonce_is_no_store(monkeypatch):
    client, _ = make_client(monkeypatch, [entry(level=1)], notes_tools())
    tag = compute_guard_hmac(KEY, "GET", H, "/__nonce", "", b"")
    r = client.get("/__nonce", headers={"Host": H, "WebSpec-Guard": tag})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"


def test_an_unknown_method_on_the_root_gets_its_allow(notes):
    client, _ = notes
    r = send(client, "TRACE", "/")
    assert answer(r) == (405, "method_not_allowed") and r.headers["allow"] == "HEAD, OPTIONS, GET"


def test_a_closed_key_is_still_described_and_its_503_names_both_tools(monkeypatch, tmp_path):
    stricter = tool("create_email", read_only=False, destructive=False, open_world=True, tier="dangerous")
    client, pool = make_client(monkeypatch, [entry(level=0)], [stricter])
    assert send(client, "POST", "/email", body=b"{}", headers={"WebSpec-Definer": "CREATE"}).status_code == 200
    pool.tools = [tool("add_email", read_only=False, destructive=False, open_world=False)]
    r = send(client, "POST", "/email", body=b"{}", headers={"WebSpec-Definer": "CREATE"})
    assert answer(r) == (503, "binding_changed")
    assert (r.json()["pinned_tool"], r.json()["tool"]) == ("create_email", "add_email")
    assert client.head("/email", headers={"Host": H}).headers["allow"] == "HEAD, OPTIONS, POST"
    options = client.options("/email", headers={"Host": H})
    assert options.status_code == 200 and options.headers["allow"] == "HEAD, OPTIONS, POST"
    assert options.json()["entries"] == [{"path": "/email", "method": "POST", "definer": "CREATE",
                                          "error": "binding_changed"}]
    assert pool.calls == [("svc", "create_email", {})]

    # AU-5: one record, when the rebuild first finds the change, describing both tools.
    send(client, "POST", "/email", body=b"{}", headers={"WebSpec-Definer": "CREATE"})
    [record] = [e for e in audit_records(tmp_path) if e["outcome"] == "binding_changed"]
    assert (record["tool"], record["tier"], record["risk"]) == ("add_email", "sensitive", 1)
    assert (record["pinned_tool"], record["pinned_tier"], record["pinned_risk"]) == ("create_email", "dangerous", 1)


ADDITIVE = ToolContract(read_only=False, destructive=False, idempotent=False, open_world=False)
READ_ONLY = ToolContract(read_only=True, destructive=False, idempotent=True, open_world=False)


def test_keys_are_compared_trimmed_and_unreachable_entries_are_logged(caplog):
    with caplog.at_level(logging.INFO):
        m = routes.RouteMap.build("w", {"x": ADDITIVE, "x/": ADDITIVE, "get_a/b": READ_ONLY})
    # Both entries are already at their full names, which trim to the same key: neither can move.
    assert m.resolve("POST", "/x", "INVOKE").error == "route_collision"
    assert m.resolve("POST", "/x/", "INVOKE").error == "route_collision"
    assert m.resolve("GET", "/a/b").error == "tool_not_found"
    assert any("get_a/b" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


def test_a_bindings_entry_may_return_a_moved_tool_to_its_key():
    first = routes.RouteMap.build("t", {"list_issues": READ_ONLY, "search_issues": READ_ONLY})
    m = first.rebuild({"list_issues": READ_ONLY, "search_issues": READ_ONLY}, {"GET /issues": "search_issues"})
    assert m.routes() == {"GET /issues": "search_issues", "GET /list_issues": "list_issues"}


@pytest.mark.parametrize("bindings", [["POST /email CREATE"], "add_email", {"POST /email CREATE": 5},
                                      {"POST /email CREATE": ["add_email"]}])
def test_malformed_bindings_confirm_nothing_and_are_logged(tmp_path, caplog, bindings):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mcpServers": {"svc": {"command": "true", "bindings": bindings}}}))
    with caplog.at_level(logging.ERROR):
        assert parse_claude_config(config)["svc"].bindings == {}
    assert any("bindings" in r.getMessage() for r in caplog.records)
