"""Regression tests for the method-profiles review findings (one or more per finding)."""

import asyncio
import json
import os
import sys
from unittest.mock import patch

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, Tool, ToolAnnotations

from webspec import approval as approval_mod
from webspec import audit, handlers
from webspec.guard import compute_guard_hmac, loads_strict
from webspec.idempotency import IdempotencyStore, StoredResponse, snapshot_response

from tests._gateway import KEY, bookend, clearance, entry, make_client, request, tool

H = "svc.localhost"


def _tools():
    typed = Tool(name="search", description="typed read", annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
                 inputSchema={"type": "object", "properties": {
                     "q": {"type": "string"}, "limit": {"type": "integer"},
                     "ids": {"type": "array", "items": {"type": "string"}}}})
    return [
        tool("read_thing", read_only=True, open_world=False),
        tool("send_thing", read_only=False, destructive=False, open_world=False),
        tool("send_mail", read_only=False, destructive=False, open_world=True),
        tool("purge", read_only=False, destructive=True, idempotent=False, open_world=False),
        typed,
    ]


def _audit_lines(tmp_path):
    log = tmp_path / "audit.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# ── A2: one argument, one value ──

def test_duplicate_query_keys_are_refused_and_audited(monkeypatch, tmp_path):
    client, pool = make_client(monkeypatch, [entry(level=1)], _tools())
    r = request(client, "GET", H, "/read_thing", query="id=1&id=2", guarded=True)
    assert r.status_code == 400 and r.json()["error"] == "duplicate_query_key"
    assert pool.calls == []
    assert _audit_lines(tmp_path)[-1]["reason"] == "duplicate_query_key"


# ── A3: the Idempotency-Key follows the tool, so DELETE can't dodge it ──

def test_delete_on_a_non_idempotent_tool_needs_and_honors_a_key(monkeypatch):
    client, pool = make_client(monkeypatch, [entry(level=2)], _tools())
    hdr = {"X-Gimme-Definer": bookend("DELETE", "PURGE", b"")}
    r = request(client, "DELETE", H, "/purge", query="older_than=30", guarded=True, headers=hdr)
    assert r.json()["error"] == "idempotency_key_required"
    hdr["Idempotency-Key"] = "p-1"
    first = request(client, "DELETE", H, "/purge", query="older_than=30", guarded=True, headers=hdr)
    again = request(client, "DELETE", H, "/purge", query="older_than=30", guarded=True, headers=hdr)
    assert first.status_code == 200 and again.headers["idempotent-replayed"] == "true"
    assert len(pool.calls) == 1


# ── A9: the guard signs the definer and the Idempotency-Key ──

def test_definer_and_key_cannot_be_swapped_in_flight(monkeypatch):
    client, pool = make_client(monkeypatch, [entry(level=1)], _tools())
    body = b'{"to":"x"}'
    boot = compute_guard_hmac(KEY, "GET", H, "/__nonce", "", b"")
    nonce = client.get("/__nonce", headers={"Host": H, "X-WebSpec-Guard": boot}).json()["nonce"]
    mac = compute_guard_hmac(KEY, "POST", H, "/send_thing", nonce, body, definer="SEND", idempotency_key="k")
    r = client.post("/send_thing", content=body, headers={
        "Host": H, "X-WebSpec-Guard": mac, "X-WebSpec-Nonce": nonce,
        "X-Gimme-Definer": "CREATE", "Idempotency-Key": "k"})  # verb swapped after signing
    assert r.status_code == 403 and r.json()["error"] == "guard_invalid"
    assert pool.calls == []


# ── A5: clearance is bound to the destination + method, and single-use ──

def test_clearance_is_bound_and_single_use(monkeypatch):
    client, pool = make_client(monkeypatch, [entry("svc", level=3), entry("other", level=3)], _tools())
    body = b'{"to":"x"}'

    def post(host, key, clr):
        return request(client, "POST", host, "/send_thing", body=body, guarded=True, headers={
            "X-Gimme-Definer": bookend("POST", "SEND", body), "Idempotency-Key": key, "X-UFO-Clearance": clr})

    clr = clearance("send_thing", {"to": "x"}, service="svc", method="POST")
    assert post("other.localhost", "k1", clr).json()["error"] == "clearance_invalid"  # wrong destination
    assert post(H, "k2", clr).status_code == 200
    assert post(H, "k3", clr).json()["error"] == "clearance_reused"                  # single-use
    assert len(pool.calls) == 1


# ── A6: level 4 witnesses open-world mutations (the exfiltration step) ──

def test_level4_challenges_an_open_world_send(monkeypatch):
    monkeypatch.delenv("WEBSPEC_APPROVERS_FILE", raising=False)
    client, pool = make_client(monkeypatch, [entry(level=4)], _tools())
    body = b'{"to":"attacker@example.com"}'
    r = request(client, "POST", H, "/send_mail", body=body, guarded=True, headers={
        "X-Gimme-Definer": bookend("POST", "SEND", body), "Idempotency-Key": "m-1",
        "X-UFO-Clearance": clearance("send_mail", {"to": "attacker@example.com"}, method="POST")})
    assert r.status_code == 428 and pool.calls == []


# ── A7: challenges can't be flushed out; retries reuse the pending one ──

def test_approval_queue_never_evicts_pending_challenges():
    store = approval_mod.ApprovalStore(max_pending=2)
    s1 = approval_mod.request_summary("DELETE", "svc", H, "/purge", "purge", {"id": "1"}, b"")
    first = store.issue(s1)
    assert store.issue(s1)["challenge"] == first["challenge"]  # same request → same challenge
    assert store.issue(approval_mod.request_summary("DELETE", "svc", H, "/purge", "purge", {"id": "2"}, b"")) is not None
    assert store.issue(approval_mod.request_summary("DELETE", "svc", H, "/purge", "purge", {"id": "3"}, b"")) is None
    assert first["challenge"] in store._challenges


def test_failed_signatures_burn_a_challenge(monkeypatch, tmp_path):
    signers = tmp_path / "allowed"
    signers.write_text("")
    monkeypatch.setenv("WEBSPEC_APPROVERS_FILE", str(signers))
    store = approval_mod.ApprovalStore()
    ch = store.issue(approval_mod.request_summary("DELETE", "svc", H, "/p", "p", {}, b""))

    async def bad(*a, **k):
        return False
    monkeypatch.setattr(approval_mod, "_ssh_verify", bad)
    header = f"{ch['challenge']}:U1NIU0lH"
    for _ in range(approval_mod.MAX_FAILED_ATTEMPTS):
        assert asyncio.run(store.verify(header, ch["fingerprint"])) == "approval_invalid"
    assert asyncio.run(store.verify(header, ch["fingerprint"])) == "approval_unknown"


def test_cancelled_verification_does_not_strand_the_challenge(monkeypatch, tmp_path):
    signers = tmp_path / "allowed"
    signers.write_text("")
    monkeypatch.setenv("WEBSPEC_APPROVERS_FILE", str(signers))
    store = approval_mod.ApprovalStore()
    ch = store.issue(approval_mod.request_summary("DELETE", "svc", H, "/p", "p", {}, b""))

    async def hang(*a, **k):
        await asyncio.sleep(3600)
    monkeypatch.setattr(approval_mod, "_ssh_verify", hang)

    async def scenario():
        task = asyncio.create_task(store.verify(f"{ch['challenge']}:U1NIU0lH", ch["fingerprint"]))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert store._challenges[ch["challenge"]].state == approval_mod.PENDING


# ── B11/B13: strict bodies; nothing runs on a body we can't faithfully read ──

@pytest.mark.parametrize("body", [b'{"older_than": 30,}', b'[1,2]', b'{"x": NaN}', b'{"x": 1e400}', b'"str"'])
def test_malformed_bodies_are_refused_not_dropped(monkeypatch, body):
    client, pool = make_client(monkeypatch, [entry(level=0)], _tools())
    r = request(client, "POST", H, "/send_thing", body=body, headers={"X-Gimme-Definer": "SEND"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_arguments"
    assert pool.calls == []


def test_loads_strict():
    assert loads_strict('{"a": 1.5}') == {"a": 1.5}
    for bad in ("NaN", "Infinity", "-Infinity", "1e400", '{"a": -1e999}'):
        with pytest.raises(ValueError):
            loads_strict(bad)


# ── B15: typed arguments through GET ──

def test_get_arguments_are_typed_by_the_input_schema(monkeypatch):
    client, pool = make_client(monkeypatch, [entry(level=0)], _tools())
    r = request(client, "GET", H, "/search", query='q=42&limit=5&ids=%5B%22a%22%2C%22b%22%5D')
    assert r.status_code == 200
    assert pool.calls[-1][2] == {"q": "42", "limit": 5, "ids": ["a", "b"]}
    r = request(client, "GET", H, "/search", query="limit=five")
    assert r.status_code == 400 and r.json()["error"] == "invalid_arguments"


def test_an_argument_cannot_come_from_both_query_and_body(monkeypatch):
    client, pool = make_client(monkeypatch, [entry(level=0)], _tools())
    r = request(client, "POST", H, "/send_thing", query="to=a", body=b'{"to":"b"}', headers={"X-Gimme-Definer": "SEND"})
    assert r.status_code == 400 and pool.calls == []


# ── B11: failures after the tool ran settle the key as unknown, and are audited ──

def test_unserializable_result_marks_the_key_unknown(monkeypatch, tmp_path):
    client, pool = make_client(monkeypatch, [entry(level=2)], _tools())
    body = b'{"to":"x"}'
    hdrs = {"X-Gimme-Definer": bookend("POST", "SEND", body), "Idempotency-Key": "u-1"}
    with patch.object(handlers, "serialize_tool_result", side_effect=TypeError("boom")):
        r = request(client, "POST", H, "/send_thing", body=body, guarded=True, headers=hdrs)
    assert r.status_code == 500 and r.json()["error"] == "result_unserializable"
    r = request(client, "POST", H, "/send_thing", body=body, guarded=True, headers=hdrs)
    assert r.status_code == 409 and r.json()["error"] == "idempotency_outcome_unknown"
    assert len(pool.calls) == 1
    assert any(e["reason"] == "result_unserializable" and e["outcome"] == "invoked" for e in _audit_lines(tmp_path))


def test_a_protocol_rejection_frees_the_key(monkeypatch):
    client, pool = make_client(monkeypatch, [entry(level=2)], _tools())
    body = b'{"to":"x"}'
    hdrs = {"X-Gimme-Definer": bookend("POST", "SEND", body), "Idempotency-Key": "r-1"}
    pool.raise_on_call = McpError(ErrorData(code=-32602, message="invalid params"))
    r = request(client, "POST", H, "/send_thing", body=body, guarded=True, headers=hdrs)
    assert r.status_code == 502 and r.json()["error"] == "tool_rejected"
    pool.raise_on_call = None
    assert request(client, "POST", H, "/send_thing", body=body, guarded=True, headers=hdrs).status_code == 200


# ── B14: decisions made before a tool is resolved are audited too ──

def test_early_denials_are_audited(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, [entry(level=1)], _tools())
    client.get("/read_thing", headers={"Host": H, "X-WebSpec-Guard": "00000000", "X-WebSpec-Nonce": "x"})
    request(client, "GET", H, "/no_such_tool", guarded=True)
    request(client, "GET", "eu.svc.localhost", "/read_thing")
    reasons = [e["reason"] for e in _audit_lines(tmp_path)]
    assert "guard_invalid" in reasons and "tool_not_found" in reasons and "unknown_qualifier" in reasons
    assert audit.verify_chain(tmp_path / "audit.jsonl") is None


# ── B16: idempotency store ordering, capacity, and limits ──

def test_eviction_takes_the_oldest_settled_record():
    s = IdempotencyStore(max_entries=3)
    s.begin("s", "A", "f")                       # slow request starts first…
    for k in "BC":
        s.begin("s", k, "f")
        s.complete("s", k, StoredResponse(200, b"", None))
    s.complete("s", "A", StoredResponse(200, b"", None))  # …and finishes last
    s.begin("s", "D", "f")
    assert s.peek("s", "A", "f").kind == "replay"   # most recently settled: kept
    assert s.peek("s", "B", "f").kind == "proceed"  # oldest settled: evicted


def test_unknown_and_in_flight_records_are_never_evicted():
    s = IdempotencyStore(max_entries=2)
    s.begin("s", "A", "f")
    s.mark_unknown("s", "A")
    s.begin("s", "B", "f")
    assert s.begin("s", "C", "f").kind == "full"
    assert s.peek("s", "A", "f").kind == "unknown"


def test_stale_in_flight_becomes_unknown():
    s = IdempotencyStore(in_flight_ttl=-1)
    s.begin("s", "A", "f")
    assert s.peek("s", "A", "f").kind == "unknown"


def test_large_results_are_not_replayed():
    stored = snapshot_response(200, b"x" * (300 * 1024), "application/json", {})
    assert stored.body is None


# ── C: config reload keeps the last good registry ──

def test_reload_keeps_last_good_config(tmp_path):
    from webspec.config import ServiceRegistry
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"svc": {"command": "x", "level": 3}}}))
    reg = ServiceRegistry(cfg)
    assert reg.get("svc").level == 3
    cfg.write_text('{"mcpServers": {"svc": ')  # half-written file
    os.utime(cfg, (1, 1))
    reg.check_reload()
    assert reg.get("svc") is not None and reg.get("svc").level == 3


# ── A1: op-auth flag allowlist ──

def test_op_run_flag_allowlist_closes_short_flag_bypasses():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "op-auth"))
    from ufo import is_run_allowed
    for args in (["get", "d", "-o/home/u/.bashrc"], ["get", "d", "-fo/tmp/x"],
                 ["get", "d", "--vault", "V", "--file-mode=0755"], ["get", "d", "--account", "other"],
                 ["get", "d", "--vault", "-o/x"], ["get", "d", "--vault"]):
        assert is_run_allowed("document", args) is False, args
    assert is_run_allowed("item", ["get", "MyItem", "--vault", "V", "--fields", "password"]) is True
    assert is_run_allowed("item", ["get", "MyItem", "--vault=V"]) is True


# ── audit rotation ──

def test_audit_chain_continues_across_a_renamed_segment(tmp_path, monkeypatch):
    log = tmp_path / "a.jsonl"
    monkeypatch.setenv("WEBSPEC_AUDIT_LOG", str(log))
    monkeypatch.setattr(audit, "_state", {})
    ctx = audit.Context(service="s", host="h", method="GET", path="/x")
    audit.record(ctx, outcome="invoked", status=200, reason=None)
    import hashlib
    last = hashlib.sha256(log.read_bytes().splitlines()[-1]).hexdigest()
    log.rename(tmp_path / "a.1.jsonl")
    audit.record(ctx, outcome="invoked", status=200, reason=None)
    assert audit.verify_chain(log) == 1                    # alone, a segment doesn't start at genesis
    assert audit.verify_chain(log, first_prev=last) is None


# ── B10: the approver reads a real terminal ──

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty")
def test_approver_confirm_works_on_a_real_pty():
    """Review finding: text-mode "r+" on /dev/tty fails on every real terminal.

    Runs in a clean subprocess: forking inside pytest's own process can deadlock.
    """
    import subprocess
    script = os.path.join(os.path.dirname(__file__), "fixtures", "pty_confirm.py")
    result = subprocess.run([sys.executable, script], timeout=30, capture_output=True)
    assert result.returncode == 0, result
