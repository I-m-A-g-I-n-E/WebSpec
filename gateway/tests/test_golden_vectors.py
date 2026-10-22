"""Golden vectors: the signed and fingerprinted bytes do not change in WebSpec v2.

v2 renames every header to WebSpec-*, retires the bookend and routes by noun, but no header
name enters any of these messages, so the guard tag (GD-2, GD-4), the clearance tag (CL-1, with
its "ufo2:" prefix), the approval fingerprint and sign message (AP-1) and the idempotency
fingerprint (ID-2) keep their byte formats. Each vector is computed three ways that must agree
with a fixed literal: from the spec's formula written out here with the standard library, by the
gateway's own function, and, for the tags a client makes, by the reference shim
(examples/shim.py). The key is the walkthrough's demo key.

The first vectors are the ones docs/guide/walkthrough.md and docs/spec/levels.md published
before v2 (a signed read, two clearances, two approval fingerprints). The others use v2's paths
and tool names (`/note`, `/email`, `send_email`), and a guard tag with no definer at all, as a
DELETE without one is signed. Vectors that contained a bookend are the only ones v2 drops, so
none appears here. These tests pass on the code before v2 and must still pass after it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

from webspec.approval import request_summary, sign_message, summary_fingerprint
from webspec.guard import canonical_query, compute_clearance_token, compute_guard_hmac
from webspec.idempotency import request_fingerprint

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import shim  # noqa: E402

DEMO_KEY = bytes.fromhex("5e" * 32)  # examples/walkthrough.py DEMO_KEY
HOST, DESTINATION = "notes.localhost", "notes"
NOTE = {"id": "welcome", "to": "ana@example.com"}
NOTE_BODY = b'{"id":"welcome","to":"ana@example.com"}'
TS = "1791244639"


# ── The spec's formulas, standard library only ──

def spec_tag(message: bytes) -> str:
    """GD-2 / CL-1: hex of the first 4 bytes of HMAC-SHA256(key, message)."""
    return hmac.new(DEMO_KEY, message, hashlib.sha256).digest()[:4].hex()


def spec_canonical_json(obj) -> str:
    """CL-1: keys sorted, "," and ":" with no whitespace, non-ASCII escaped."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def spec_guard_message(method, host, path, nonce, body, query="", definer="", idempotency_key="") -> bytes:
    """GD-2: METHOD:host:path:nonce:hex(sha256(body)) [:?query] [:!definer] [:#idempotency-key]."""
    message = f"{method}:{host}:{path}:{nonce}:{hashlib.sha256(body).hexdigest()}"
    if query:
        message += f":?{query}"
    if definer:
        message += f":!{definer}"
    if idempotency_key:
        message += f":#{idempotency_key}"
    return message.encode()


def spec_clearance_message(destination, method, tool, args, ts) -> bytes:
    """CL-1: "ufo2:" destination ":" METHOD ":" tool ":" canonical-args ":" ts."""
    return f"ufo2:{destination}:{method}:{tool}:{spec_canonical_json(args)}:{ts}".encode()


def spec_summary(method, path, tool, args, body) -> dict:
    """AP-1's summary: what the approver agrees to."""
    return {"v": 1, "method": method, "service": DESTINATION, "host": HOST, "path": path, "tool": tool,
            "args": args, "body_sha256": hashlib.sha256(body).hexdigest()}


def spec_idempotency_fingerprint(method, path, query, body) -> str:
    """ID-2: SHA-256 of the method, path, canonical query and body hash, each followed by a newline."""
    h = hashlib.sha256()
    for part in (method, path, query, hashlib.sha256(body).hexdigest()):
        h.update(part.encode() + b"\n")
    return h.hexdigest()


# ── GD-2 / GD-4: the guard tag ──

GUARD = {
    # docs/guide/walkthrough.md, level 1, "Read, signed"
    "published signed read": (dict(method="GET", host=HOST, path="/read_note",
                                   nonce="403c28aba42fa462a23469b97db9b204", body=b"", query="id=welcome"),
                              "ef56efa3"),
    # GD-4: GET /__nonce is signed with an empty nonce
    "nonce bootstrap": (dict(method="GET", host=HOST, path="/__nonce", nonce="", body=b""), "ca89199e"),
    # v2: a POST names its action with the definer, which the guard signs raw
    "v2 send": (dict(method="POST", host=HOST, path="/email", nonce="6ebaa50f4841c06f55ad868cf8878d7a",
                     body=NOTE_BODY, definer="SEND", idempotency_key="k-7f3a"), "2edd8234"),
    # v2: a DELETE without a definer signs no ":!" part at all
    "v2 delete, no definer": (dict(method="DELETE", host=HOST, path="/note",
                                   nonce="865692949b1057e4835adca62d4597ab", body=b"", query="id=welcome"),
                              "941d888a"),
    "v2 delete, REMOVE": (dict(method="DELETE", host=HOST, path="/note", nonce="865692949b1057e4835adca62d4597ab",
                               body=b"", query="id=welcome", definer="REMOVE"), "1952c324"),
}


@pytest.mark.parametrize("name", list(GUARD))
def test_guard_tag(name):
    inputs, expected = GUARD[name]
    query = canonical_query(inputs.get("query", ""))
    spec_inputs = {**inputs, "query": query}
    assert spec_tag(spec_guard_message(**spec_inputs)) == expected
    assert compute_guard_hmac(DEMO_KEY, inputs["method"], inputs["host"], inputs["path"], inputs["nonce"],
                              inputs["body"], query, definer=inputs.get("definer", ""),
                              idempotency_key=inputs.get("idempotency_key", "")) == expected
    assert shim.guard_tag(DEMO_KEY, inputs["method"], inputs["host"], inputs["path"], inputs["nonce"], inputs["body"],
                          inputs.get("query", ""), inputs.get("definer", ""),
                          inputs.get("idempotency_key", "")) == expected


# ── CL-1: the clearance tag ──

CLEARANCE = {
    # docs/guide/walkthrough.md, levels 3 and 4
    "published send_note": (("POST", "send_note", NOTE), "08db7b72"),
    "published delete_note": (("DELETE", "delete_note", {"id": "welcome"}), "e899d774"),
    # v2's demo server sends with send_email (POST /email, WebSpec-Definer: SEND)
    "v2 send_email": (("POST", "send_email", NOTE), "282cda94"),
}


@pytest.mark.parametrize("name", list(CLEARANCE))
def test_clearance_tag(name):
    (method, tool, args), expected = CLEARANCE[name]
    message = spec_clearance_message(DESTINATION, method, tool, args, TS)
    assert message.startswith(b"ufo2:")  # CL-1's prefix stays, for byte stability
    assert spec_tag(message) == expected
    assert compute_clearance_token(DEMO_KEY, tool, args, TS, service=DESTINATION, method=method) == expected
    assert shim.clearance(DEMO_KEY, DESTINATION, method, tool, args, int(TS)) == f"{expected}:{TS}"


# ── AP-1: the approval fingerprint and the message the approver signs ──

APPROVAL = {
    # docs/spec/levels.md AP-1 and docs/guide/walkthrough.md, level 4
    "published send_note": (("POST", "/send_note", "send_note", NOTE, NOTE_BODY),
                            "4f935160084ad6289b3eb2b1216fd7079f09e6bddc8ea822d4507299599dbb84"),
    "published delete_note": (("DELETE", "/delete_note", "delete_note", {"id": "welcome"}, b""),
                              "fd6d65a8ecab2be78260c5b9b721b3e5de2d27b8c899c945d22728f1b5462d39"),
    "v2 send_email": (("POST", "/email", "send_email", NOTE, NOTE_BODY),
                      "a7f83b4e4407c38945721d25b3491eed7feb56b614a14484ec94a18b888782a1"),
    "v2 delete": (("DELETE", "/note", "delete_note", {"id": "welcome"}, b""),
                  "1fcc3777da37a569522691778b6218792e6a002321ce7646cb6c8ed163771171"),
}


@pytest.mark.parametrize("name", list(APPROVAL))
def test_approval_fingerprint(name):
    (method, path, tool, args, body), expected = APPROVAL[name]
    assert hashlib.sha256(spec_canonical_json(spec_summary(method, path, tool, args, body)).encode()).hexdigest() \
        == expected
    summary = request_summary(method, DESTINATION, HOST, path, tool, args, body)
    assert summary == spec_summary(method, path, tool, args, body)
    assert summary_fingerprint(summary) == expected


def test_approval_sign_message():
    fingerprint = APPROVAL["published send_note"][1]
    assert sign_message("053f02f8b18f3e5c0a8eb7762777f8be", fingerprint) == (
        "webspec-approval/v1 053f02f8b18f3e5c0a8eb7762777f8be " + fingerprint)


# ── ID-2: the idempotency fingerprint ──

IDEMPOTENCY = {
    "v2 send": (("POST", "/email", "", NOTE_BODY),
                "9601423190b3a86aca65a1c24b77bd5c852ca6fedfe637e1fd2cc2fa6e85a325"),
    "v2 delete": (("DELETE", "/note", "id=welcome", b""),
                  "05541cf00ab593fadda0669497599a8dca74294b4c83aa19428594ad1d5b832f"),
}


@pytest.mark.parametrize("name", list(IDEMPOTENCY))
def test_idempotency_fingerprint(name):
    (method, path, query, body), expected = IDEMPOTENCY[name]
    assert spec_idempotency_fingerprint(method, path, query, body) == expected
    assert request_fingerprint(method, path, query, body) == expected
