"""Idempotency-Key handling for non-idempotent methods (POST, PATCH).

Follows the semantics of the IETF HTTPAPI ``Idempotency-Key`` header draft:

- same key + same request fingerprint, completed  → replay the stored response
  (``Idempotent-Replayed: true``), the tool is NOT called again
- same key + different fingerprint                → 422 (key reused for another request)
- same key, first attempt still in flight          → 409
- same key, first attempt's outcome unknown        → 409 (the tool may have run; a blind
  retry could double-send, so the gateway refuses rather than guess)

Agents retry aggressively; this makes "send" safe to retry without making the gateway
re-execute side effects. Store is in-memory and per-process.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field

IDEMPOTENCY_TTL = 24 * 3600  # seconds a completed result is replayable
MAX_ENTRIES = 10_000
MAX_KEY_LENGTH = 255

IN_FLIGHT = "in_flight"
DONE = "done"
UNKNOWN = "unknown"

# Response headers worth replaying verbatim (everything else is regenerated).
_REPLAY_HEADER_PREFIXES = ("x-gimme-", "x-webspec-", "x-ufo-")


def valid_key(key: str) -> bool:
    """1–255 visible ASCII characters (no spaces/controls)."""
    return 0 < len(key) <= MAX_KEY_LENGTH and all(0x21 <= ord(c) <= 0x7E for c in key)


def request_fingerprint(method: str, path: str, canonical_query: str, body: bytes) -> str:
    h = hashlib.sha256()
    for part in (method.upper(), path, canonical_query, hashlib.sha256(body).hexdigest()):
        h.update(part.encode())
        h.update(b"\n")
    return h.hexdigest()


@dataclass
class StoredResponse:
    status_code: int
    body: bytes
    media_type: str | None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _Record:
    fingerprint: str
    state: str
    created_at: float
    response: StoredResponse | None = None


@dataclass(frozen=True)
class Decision:
    """Outcome of ``begin``: ``proceed`` | ``replay`` | ``mismatch`` | ``in_flight`` | ``unknown``."""

    kind: str
    response: StoredResponse | None = None


class IdempotencyStore:
    def __init__(self, ttl: float = IDEMPOTENCY_TTL, max_entries: int = MAX_ENTRIES):
        self._ttl = ttl
        self._max = max_entries
        self._records: OrderedDict[tuple[str, str], _Record] = OrderedDict()

    def _purge(self, now: float) -> None:
        for k in [k for k, r in self._records.items() if r.state != IN_FLIGHT and now - r.created_at > self._ttl]:
            del self._records[k]
        # Over capacity: evict oldest settled records; never evict in-flight ones.
        while len(self._records) >= self._max:
            victim = next((k for k, r in self._records.items() if r.state != IN_FLIGHT), None)
            if victim is None:
                break
            del self._records[victim]

    def begin(self, scope: str, key: str, fingerprint: str) -> Decision:
        """Atomically classify the key; on ``proceed`` the key is marked in-flight.

        Must be called with no ``await`` between it and the caller acting on the result.
        """
        now = time.monotonic()
        self._purge(now)
        rec = self._records.get((scope, key))
        if rec is not None:
            if rec.fingerprint != fingerprint:
                return Decision("mismatch")
            if rec.state == DONE:
                return Decision("replay", rec.response)
            return Decision(rec.state)  # in_flight | unknown
        self._records[(scope, key)] = _Record(fingerprint=fingerprint, state=IN_FLIGHT, created_at=now)
        return Decision("proceed")

    def peek(self, scope: str, key: str, fingerprint: str) -> Decision:
        """Non-mutating lookup (used to replay before re-checking one-shot credentials)."""
        rec = self._records.get((scope, key))
        if rec is None or (rec.state != IN_FLIGHT and time.monotonic() - rec.created_at > self._ttl):
            return Decision("proceed")
        if rec.fingerprint != fingerprint:
            return Decision("mismatch")
        if rec.state == DONE:
            return Decision("replay", rec.response)
        return Decision(rec.state)

    def complete(self, scope: str, key: str, response: StoredResponse) -> None:
        rec = self._records.get((scope, key))
        if rec is not None:
            rec.state = DONE
            rec.response = response
            rec.created_at = time.monotonic()

    def mark_unknown(self, scope: str, key: str) -> None:
        """The tool may or may not have run (timeout / dropped connection mid-call)."""
        rec = self._records.get((scope, key))
        if rec is not None:
            rec.state = UNKNOWN
            rec.created_at = time.monotonic()

    def abandon(self, scope: str, key: str) -> None:
        """Nothing executed (failed before the tool call) — free the key for a retry."""
        rec = self._records.get((scope, key))
        if rec is not None and rec.state == IN_FLIGHT:
            del self._records[(scope, key)]


def snapshot_response(status_code: int, body: bytes, media_type: str | None, headers) -> StoredResponse:
    kept = {k: v for k, v in headers.items() if k.lower().startswith(_REPLAY_HEADER_PREFIXES)}
    return StoredResponse(status_code=status_code, body=body, media_type=media_type, headers=kept)


store = IdempotencyStore()
