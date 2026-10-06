"""Unit tests: host grammar, idempotency store, audit chain, guard query canonicalization, hardening."""

import hashlib
import hmac
import json
import sys

import pytest

from webspec import audit
from webspec.guard import canonical_query, compute_guard_hmac, generate_nonce
from webspec.hardening import harden_process
from webspec.hostgrammar import qualifiers_allowed, split_labels
from webspec.idempotency import IdempotencyStore, StoredResponse, request_fingerprint, valid_key
from webspec.methods import ToolContract


# ── host grammar ──

def test_split_labels():
    assert split_labels("slack") == ("slack", ())
    assert split_labels("eu.v2.slack") == ("slack", ("eu", "v2"))
    assert split_labels("Slack") is None
    assert split_labels("a..b") is None
    assert split_labels("-x") is None
    assert split_labels("") is None


def test_qualifiers_allowed():
    allow = ("eu", "us", "v2")
    assert qualifiers_allowed((), ())
    assert not qualifiers_allowed(("eu",), ())          # default: no qualifiers
    assert qualifiers_allowed(("eu",), allow)
    assert qualifiers_allowed(("eu", "v2"), allow)
    assert not qualifiers_allowed(("v2", "eu"), allow)   # canonical order only
    assert not qualifiers_allowed(("eu", "eu"), allow)   # no repeats
    assert not qualifiers_allowed(("ap",), allow)        # not allow-listed


# ── idempotency ──

def test_idempotency_lifecycle():
    s = IdempotencyStore()
    fp = request_fingerprint("POST", "/send", "", b"{}")
    assert s.begin("svc", "k", fp).kind == "proceed"
    assert s.begin("svc", "k", fp).kind == "in_flight"
    s.complete("svc", "k", StoredResponse(200, b"ok", "application/json", {}))
    d = s.begin("svc", "k", fp)
    assert d.kind == "replay" and d.response.body == b"ok"
    assert s.begin("svc", "k", request_fingerprint("POST", "/send", "", b"{1}")).kind == "mismatch"
    assert s.begin("other-svc", "k", fp).kind == "proceed"  # keys are scoped per service


def test_idempotency_unknown_and_abandon():
    s = IdempotencyStore()
    fp = "f"
    s.begin("svc", "a", fp)
    s.mark_unknown("svc", "a")
    assert s.begin("svc", "a", fp).kind == "unknown"
    s.begin("svc", "b", fp)
    s.abandon("svc", "b")
    assert s.begin("svc", "b", fp).kind == "proceed"


def test_idempotency_ttl_and_capacity():
    s = IdempotencyStore(ttl=-1)
    s.begin("svc", "k", "f")
    s.complete("svc", "k", StoredResponse(200, b"", None))
    assert s.begin("svc", "k", "f").kind == "proceed"  # expired → fresh
    s = IdempotencyStore(max_entries=2)
    for k in "abc":
        s.begin("svc", k, "f")
        s.complete("svc", k, StoredResponse(200, b"", None))
    assert s.peek("svc", "a", "f").kind == "proceed"   # oldest settled evicted
    assert s.peek("svc", "c", "f").kind == "replay"


@pytest.mark.parametrize("key,ok", [("abc-123", True), ("", False), ("a b", False), ("x" * 256, False), ("é", False)])
def test_valid_key(key, ok):
    assert valid_key(key) is ok


# ── audit ──

def _ctx():
    return audit.Context(service="svc", host="svc.localhost", method="POST", path="/send", tool="send",
                         level=2, contract=ToolContract(), query="", body=b'{"secret":"hunter2"}')


def test_audit_chain_and_tamper_detection(tmp_path, monkeypatch):
    log = tmp_path / "a.jsonl"
    monkeypatch.setenv("WEBSPEC_AUDIT_LOG", str(log))
    monkeypatch.setattr(audit, "_state", {})
    for i in range(3):
        audit.record(_ctx(), outcome="invoked", status=200, reason=None)
    assert audit.verify_chain(log) is None
    text = log.read_text()
    assert "hunter2" not in text  # arguments are hashed, never logged verbatim
    lines = text.splitlines()
    tampered = json.loads(lines[1])
    tampered["outcome"] = "denied"
    lines[1] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    log.write_text("\n".join(lines) + "\n")
    assert audit.verify_chain(log) == 3  # the line after the edit no longer links


def test_audit_chain_survives_restart(tmp_path, monkeypatch):
    log = tmp_path / "a.jsonl"
    monkeypatch.setenv("WEBSPEC_AUDIT_LOG", str(log))
    monkeypatch.setattr(audit, "_state", {})
    audit.record(_ctx(), outcome="invoked", status=200, reason=None)
    monkeypatch.setattr(audit, "_state", {})  # simulate a new process
    audit.record(_ctx(), outcome="invoked", status=200, reason=None)
    assert audit.verify_chain(log) is None


def test_audit_disabled_by_empty_path(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSPEC_AUDIT_LOG", "")
    audit.record(_ctx(), outcome="invoked", status=200, reason=None)
    assert not list(tmp_path.iterdir())


# ── guard query coverage ──

def test_canonical_query_sorts_and_encodes():
    assert canonical_query("") == ""
    assert canonical_query("b=2&a=1&a=0") == "a=0&a=1&b=2"
    assert canonical_query("q=hello%20world&x=") == "q=hello%20world&x="
    assert canonical_query("q=a+b") == "q=a%20b"


def test_guard_hmac_without_query_is_backward_compatible(session_key):
    body = b"{}"
    legacy = hmac.new(session_key, f"GET:h:/p:n:{hashlib.sha256(body).hexdigest()}".encode(),
                      hashlib.sha256).digest()[:4].hex()
    assert compute_guard_hmac(session_key, "GET", "h", "/p", "n", body) == legacy
    assert compute_guard_hmac(session_key, "GET", "h", "/p", "n", body, "a=1") != legacy


# ── hardening ──

def test_harden_process_is_best_effort():
    result = harden_process()
    assert isinstance(result, bool)
    if not sys.platform.startswith("linux"):
        assert result is False


# ── approver CLI ──

import argparse

from tests._gateway import HAVE_SSH_KEYGEN, make_approver
from webspec import approval as approval_mod
from webspec import approve_cli


def _challenge():
    summary = approval_mod.request_summary("DELETE", "svc", "svc.localhost", "/delete_thing",
                                           "delete_thing", {"id": "1\x1b[2K"}, b"")
    return approval_mod.store.issue(summary)


def _run(monkeypatch, tmp_path, challenge, *, confirm=True, key=None):
    path = tmp_path / "challenge.json"
    path.write_text(json.dumps(challenge))
    shown = []
    monkeypatch.setattr(approve_cli, "_confirm", lambda prompt: confirm)
    monkeypatch.setattr(approve_cli, "_show", shown.append)
    rc = approve_cli.cmd_approve(argparse.Namespace(challenge=str(path), key=key, signer=None))
    return rc, "".join(shown)


def test_approve_refuses_a_tampered_challenge(monkeypatch, tmp_path, capsys):
    ch = _challenge()
    ch["summary"]["args"] = {"id": "harmless"}  # what the human would see ≠ what is hashed
    rc, shown = _run(monkeypatch, tmp_path, ch)
    assert rc == 2 and "tampered" in capsys.readouterr().err and shown == ""


def test_approve_escapes_control_characters(monkeypatch, tmp_path):
    rc, shown = _run(monkeypatch, tmp_path, _challenge(), confirm=False)
    assert rc == 1 and "\x1b" not in shown and "\\u001b" in shown


@pytest.mark.skipif(not HAVE_SSH_KEYGEN, reason="ssh-keygen not installed")
def test_approve_signs_and_gateway_verifies(monkeypatch, tmp_path, capsys):
    import asyncio
    key, signers = make_approver(tmp_path)
    monkeypatch.setenv("WEBSPEC_APPROVERS_FILE", str(signers))
    ch = _challenge()
    rc, _ = _run(monkeypatch, tmp_path, ch, key=str(key))
    assert rc == 0
    header = capsys.readouterr().out.strip().removeprefix("X-WebSpec-Approval: ")
    assert asyncio.run(approval_mod.store.verify(header, ch["fingerprint"])) is None


def test_nonce_expires_at_is_wall_clock():
    import time
    before = time.time()
    issued = generate_nonce("mail")
    assert issued["audience"] == "mail" and issued["ttl_seconds"] == 60
    assert before + 59 <= issued["expires_at"] <= time.time() + 61
