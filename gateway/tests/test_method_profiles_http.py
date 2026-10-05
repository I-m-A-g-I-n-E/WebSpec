"""End-to-end: per-method profiles through the real Starlette app (fake MCP pool)."""

import asyncio
import json

import pytest

from webspec import audit
from webspec.approval import sign_message

from tests._gateway import (
    HAVE_SSH_KEYGEN,
    bookend,
    clearance,
    entry,
    make_approver,
    make_client,
    request,
    ssh_sign,
    tool,
)

H = "svc.localhost"


def _tools():
    return [
        tool("read_thing", read_only=True, open_world=False),
        tool("search_web", read_only=True, open_world=True),
        tool("send_thing", read_only=False, destructive=False, open_world=False),
        tool("set_thing", read_only=False, destructive=False, idempotent=True, open_world=False),
        tool("delete_thing", read_only=False, destructive=True, idempotent=True, open_world=False),
        tool("plain", annotated=False),
        tool("read_secret", read_only=True, open_world=False, tier="sensitive"),
    ]


@pytest.fixture
def l0(monkeypatch):
    return make_client(monkeypatch, [entry(level=0)], _tools())


# ── method binding (the gateway, not the caller, picks the method class) ──

def test_get_reads_a_read_only_tool(l0):
    client, pool = l0
    r = request(client, "GET", H, "/read_thing", query="id=1")
    assert r.status_code == 200 and r.json()["result"] == {"ok": "read_thing"}
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-webspec-level"] == "0"
    assert "x-ufo-taint" not in r.headers
    assert pool.calls == [("svc", "read_thing", {"id": "1"})]


def test_open_world_output_is_marked_tainted(l0):
    client, _ = l0
    r = request(client, "GET", H, "/search_web", query="q=x")
    assert r.headers["x-ufo-taint"] == "open-world"


@pytest.mark.parametrize("tool_name,allow", [
    ("delete_thing", "HEAD, OPTIONS, PUT, DELETE"),
    ("send_thing", "HEAD, OPTIONS, POST, PATCH"),
    ("plain", "HEAD, OPTIONS, POST, DELETE"),
])
def test_get_cannot_reach_a_mutating_tool(l0, tool_name, allow):
    client, pool = l0
    r = request(client, "GET", H, f"/{tool_name}", query="to=x")
    assert r.status_code == 405
    assert r.headers["allow"] == allow
    assert r.json()["error"] == "method_not_allowed"
    assert pool.calls == []


def test_post_cannot_reach_a_read_only_tool(l0):
    client, pool = l0
    r = request(client, "POST", H, "/read_thing", body=b"{}", headers={"X-Gimme-Definer": "CREATE"})
    assert r.status_code == 405 and r.headers["allow"] == "HEAD, OPTIONS, GET"
    assert pool.calls == []


def test_get_body_is_rejected(l0):
    client, pool = l0
    r = request(client, "GET", H, "/read_thing", body=b'{"id": 1}')
    assert r.status_code == 400 and r.json()["error"] == "body_not_allowed"
    assert pool.calls == []


def test_delete_requires_a_delete_family_definer(l0):
    client, pool = l0
    assert request(client, "DELETE", H, "/delete_thing", query="id=1").json()["error"] == "missing_definer"
    r = request(client, "DELETE", H, "/delete_thing", query="id=1", headers={"X-Gimme-Definer": "SEND"})
    assert r.json()["error"] == "definer_family_mismatch"
    r = request(client, "DELETE", H, "/delete_thing", query="id=1", headers={"X-Gimme-Definer": "REMOVE"})
    assert r.status_code == 200 and r.headers["x-gimme-definer-canonical"] == "REMOVE"
    assert pool.calls == [("svc", "delete_thing", {"id": "1"})]


def test_operator_override_can_unlock_get(monkeypatch):
    client, _ = make_client(monkeypatch, [entry(tools={"plain": {"read_only": True}})], _tools())
    assert request(client, "GET", H, "/plain").status_code == 200


def test_invalid_override_fails_closed(monkeypatch):
    client, _ = make_client(monkeypatch, [entry(tools={"read_thing": {"read_only": "sure"}})], _tools())
    r = request(client, "GET", H, "/read_thing")
    assert r.status_code == 405
    info = client.options("/read_thing", headers={"Host": H}).json()
    assert info["contract"]["tier"] == "dangerous"


def test_contract_rug_pull_is_blocked(l0):
    client, pool = l0
    assert request(client, "GET", H, "/read_thing").status_code == 200
    # Server re-lists the tool as destructive: tightening takes effect immediately.
    pool.tools = [tool("read_thing", read_only=False, destructive=True)] + pool.tools[1:]
    assert request(client, "GET", H, "/read_thing").status_code == 405
    # Server flips it back to read-only: loosening is NOT accepted (pinned).
    pool.tools = [tool("read_thing", read_only=True)] + pool.tools[1:]
    assert request(client, "GET", H, "/read_thing").status_code == 405


# ── discovery tells the AI exactly what to send ──

def test_options_and_head_describe_the_profile(l0):
    client, _ = l0
    one = client.options("/delete_thing", headers={"Host": H})
    assert one.status_code == 200 and one.headers["allow"] == "HEAD, OPTIONS, PUT, DELETE"
    body = one.json()
    assert body["methods"] == ["DELETE", "PUT"]
    assert set(body["scopes_required"]) == {"DELETE", "PUT"}
    assert body["requirements"]["DELETE"]["definer"] is True
    every = client.options("/", headers={"Host": H}).json()
    assert {t["name"]: t["methods"] for t in every["tools"]}["read_thing"] == ["GET"]
    head = client.head("/send_thing", headers={"Host": H})
    assert head.headers["allow"] == "HEAD, OPTIONS, POST, PATCH"
    listing = client.get("/", headers={"Host": H}).json()
    assert {t["name"]: t["methods"] for t in listing["tools"]}["plain"] == ["DELETE", "POST"]


# ── level 1: the guard signs the query too ──

def test_guard_covers_the_query_string(monkeypatch):
    client, pool = make_client(monkeypatch, [entry(level=1)], _tools())
    ok = request(client, "GET", H, "/read_thing", query="id=1&b=2", guarded=True)
    assert ok.status_code == 200
    forged = request(client, "GET", H, "/read_thing", query="id=999", guarded=True, sign_query="id=1")
    assert forged.status_code == 403 and forged.json()["error"] == "guard_invalid"
    assert [c[2] for c in pool.calls] == [{"id": "1", "b": "2"}]


def test_challenge_endpoint_is_retired(monkeypatch):
    from webspec.guard import compute_guard_hmac
    from tests._gateway import KEY
    client, _ = make_client(monkeypatch, [entry(level=1)], _tools())
    mac = compute_guard_hmac(KEY, "GET", H, "/__challenge", "", b"")
    r = client.get("/__challenge?tool=run", headers={"Host": H, "X-WebSpec-Guard": mac})
    assert r.status_code == 410


# ── host grammar: qualifiers route, never authenticate ──

def test_qualifier_labels(monkeypatch):
    client, pool = make_client(monkeypatch, [entry(level=1, labels=("eu", "v2"))], _tools())
    # Allowed by grammar, but there is no per-qualifier backend yet: fail closed rather than
    # serve "eu" from the default backend (review finding: false sense of residency).
    r = request(client, "GET", "eu.v2.svc.localhost", "/read_thing")
    assert r.status_code == 404 and r.json()["error"] == "qualifier_not_routable"
    assert pool.calls == []
    r = request(client, "GET", "v2.eu.svc.localhost", "/read_thing")
    assert r.status_code == 404 and r.json()["error"] == "unknown_qualifier"
    assert request(client, "GET", "ap.svc.localhost", "/read_thing").status_code == 404
    assert request(client, "GET", "EU.svc.localhost", "/read_thing").json()["error"] == "invalid_host"


def test_qualifiers_rejected_by_default(l0):
    client, _ = l0
    assert request(client, "GET", "eu.svc.localhost", "/read_thing").status_code == 404


# ── level 2: bookend + idempotency ──

@pytest.fixture
def l2(monkeypatch):
    return make_client(monkeypatch, [entry(level=2)], _tools())


def test_level2_requires_bookend_and_idempotency_key(l2):
    client, pool = l2
    body = b'{"to":"x"}'
    r = request(client, "POST", H, "/send_thing", body=body, guarded=True, headers={"X-Gimme-Definer": "SEND"})
    assert r.status_code == 403 and r.json()["error"] == "bookend_required"
    r = request(client, "POST", H, "/send_thing", body=body, guarded=True,
                headers={"X-Gimme-Definer": bookend("POST", "SEND", body)})
    assert r.json()["error"] == "idempotency_key_required"
    assert pool.calls == []


def test_idempotent_replay_does_not_resend(l2):
    client, pool = l2
    body = b'{"to":"x"}'
    hdrs = {"X-Gimme-Definer": bookend("POST", "SEND", body), "Idempotency-Key": "k-1"}
    first = request(client, "POST", H, "/send_thing", body=body, guarded=True, headers=hdrs)
    again = request(client, "POST", H, "/send_thing", body=body, guarded=True, headers=hdrs)
    assert first.status_code == again.status_code == 200
    assert again.headers["idempotent-replayed"] == "true" and again.content == first.content
    assert len(pool.calls) == 1
    other = b'{"to":"y"}'
    r = request(client, "POST", H, "/send_thing", body=other, guarded=True,
                headers={"X-Gimme-Definer": bookend("POST", "SEND", other), "Idempotency-Key": "k-1"})
    assert r.status_code == 422 and r.json()["error"] == "idempotency_key_reused"


def test_timeout_makes_the_outcome_unknown(l2):
    client, pool = l2
    body = b"{}"
    hdrs = {"X-Gimme-Definer": bookend("POST", "SEND", body), "Idempotency-Key": "k-2"}
    pool.raise_on_call = asyncio.TimeoutError()
    assert request(client, "POST", H, "/send_thing", body=body, guarded=True, headers=hdrs).status_code == 504
    pool.raise_on_call = None
    r = request(client, "POST", H, "/send_thing", body=body, guarded=True, headers=hdrs)
    assert r.status_code == 409 and r.json()["error"] == "idempotency_outcome_unknown"
    assert len(pool.calls) == 1


def test_put_is_idempotent_so_needs_no_key(l2):
    client, _ = l2
    body = b'{"v":1}'
    r = request(client, "PUT", H, "/set_thing", body=body, guarded=True,
                headers={"X-Gimme-Definer": bookend("PUT", "SET", body)})
    assert r.status_code == 200


# ── level 3: clearance ──

def test_level3_requires_clearance_for_mutations_and_sensitive_reads(monkeypatch):
    client, pool = make_client(monkeypatch, [entry(level=3)], _tools())
    assert request(client, "GET", H, "/read_thing", guarded=True).status_code == 200  # open read: none needed
    r = request(client, "GET", H, "/read_secret", query="ref=a", guarded=True)
    assert r.status_code == 403 and r.json()["error"] == "clearance_missing"
    r = request(client, "GET", H, "/read_secret", query="ref=a", guarded=True,
                headers={"X-UFO-Clearance": clearance("read_secret", {"ref": "a"}, method="GET")})
    assert r.status_code == 200
    r = request(client, "GET", H, "/read_secret", query="ref=b", guarded=True,
                headers={"X-UFO-Clearance": clearance("read_secret", {"ref": "a"}, method="GET")})
    assert r.json()["error"] == "clearance_invalid"  # bound to the exact arguments
    body = b'{"to":"x"}'
    hdrs = {"X-Gimme-Definer": bookend("POST", "SEND", body), "Idempotency-Key": "c-1"}
    r = request(client, "POST", H, "/send_thing", body=body, guarded=True, headers=hdrs)
    assert r.json()["error"] == "clearance_missing"
    hdrs["X-UFO-Clearance"] = clearance("send_thing", {"to": "x"}, method="POST")
    assert request(client, "POST", H, "/send_thing", body=body, guarded=True, headers=hdrs).status_code == 200


# ── level 4: a human signs the exact request ──

@pytest.mark.skipif(not HAVE_SSH_KEYGEN, reason="ssh-keygen not installed")
def test_level4_human_approval_round_trip(monkeypatch, tmp_path):
    key, signers = make_approver(tmp_path)
    monkeypatch.setenv("WEBSPEC_APPROVERS_FILE", str(signers))
    client, pool = make_client(monkeypatch, [entry(level=4)], _tools())

    def attempt(query, approval=None):
        hdrs = {"X-Gimme-Definer": bookend("DELETE", "REMOVE", b""),
                "X-UFO-Clearance": clearance("delete_thing", dict(p.split("=") for p in query.split("&")),
                                             method="DELETE")}
        if approval:
            hdrs["X-WebSpec-Approval"] = approval
        return request(client, "DELETE", H, "/delete_thing", query=query, guarded=True, headers=hdrs)

    challenge = attempt("id=1")
    assert challenge.status_code == 428
    ch = challenge.json()
    assert ch["summary"]["tool"] == "delete_thing" and ch["summary"]["args"] == {"id": "1"}
    assert pool.calls == []

    approval = f"{ch['challenge']}:{ssh_sign(key, sign_message(ch['challenge'], ch['fingerprint']))}"
    assert attempt("id=2", approval).json()["error"] == "approval_mismatch"  # can't reuse for other args
    ok = attempt("id=1", approval)
    assert ok.status_code == 200, ok.text
    assert pool.calls == [("svc", "delete_thing", {"id": "1"})]
    assert attempt("id=1", approval).json()["error"] == "approval_reused"  # single-use


@pytest.mark.skipif(not HAVE_SSH_KEYGEN, reason="ssh-keygen not installed")
def test_level4_rejects_a_signature_from_an_unlisted_key(monkeypatch, tmp_path):
    (tmp_path / "trusted").mkdir()
    (tmp_path / "rogue").mkdir()
    _, signers = make_approver(tmp_path / "trusted")
    rogue, _ = make_approver(tmp_path / "rogue")
    monkeypatch.setenv("WEBSPEC_APPROVERS_FILE", str(signers))
    client, pool = make_client(monkeypatch, [entry(level=4)], _tools())
    hdrs = {"X-Gimme-Definer": bookend("DELETE", "REMOVE", b""),
            "X-UFO-Clearance": clearance("delete_thing", {"id": "1"}, method="DELETE")}
    ch = request(client, "DELETE", H, "/delete_thing", query="id=1", guarded=True, headers=hdrs).json()
    hdrs["X-WebSpec-Approval"] = f"{ch['challenge']}:{ssh_sign(rogue, sign_message(ch['challenge'], ch['fingerprint']))}"
    r = request(client, "DELETE", H, "/delete_thing", query="id=1", guarded=True, headers=hdrs)
    assert r.status_code == 403 and r.json()["error"] == "approval_invalid"
    assert pool.calls == []


def test_level4_without_approvers_fails_closed(monkeypatch):
    monkeypatch.delenv("WEBSPEC_APPROVERS_FILE", raising=False)
    client, pool = make_client(monkeypatch, [entry(level=4)], _tools())
    hdrs = {"X-Gimme-Definer": bookend("DELETE", "REMOVE", b""),
            "X-UFO-Clearance": clearance("delete_thing", {"id": "1"}, method="DELETE")}
    ch = request(client, "DELETE", H, "/delete_thing", query="id=1", guarded=True, headers=hdrs).json()
    hdrs["X-WebSpec-Approval"] = f"{ch['challenge']}:U1NIU0lH"  # base64("SSHSIG")
    r = request(client, "DELETE", H, "/delete_thing", query="id=1", guarded=True, headers=hdrs)
    assert r.status_code == 503 and r.json()["error"] == "approval_unavailable"
    assert pool.calls == []


# ── audit ──

def test_every_decision_is_audited_and_chained(l0, tmp_path):
    client, _ = l0
    request(client, "GET", H, "/read_thing")
    request(client, "GET", H, "/delete_thing")
    log = tmp_path / "audit.jsonl"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["tool"], e["outcome"], e["reason"]) for e in entries] == [
        ("read_thing", "invoked", None),
        ("delete_thing", "denied", "method_not_allowed"),  # a GET probe at a destructive tool is evidence
    ]
    assert audit.verify_chain(log) is None
