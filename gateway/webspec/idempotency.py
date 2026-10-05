"""Idempotency-Key handling for non-idempotent tools.

Follows the semantics of the IETF HTTPAPI ``Idempotency-Key`` header draft:

- same key + same request fingerprint, completed  → replay the stored response
  (``Idempotent-Replayed: true``), the tool is NOT called again
- same key + different fingerprint                → 422 (key reused for another request)
- same key, first attempt still in flight          → 409
- same key, first attempt's outcome unknown        → 409 (the tool may have run; a blind
  retry could double-send, so the gateway refuses rather than guess)

Agents retry aggressively; this makes "send" safe to retry without making the gateway
re-execute side effects.

Limits (spec §7): the store is in-memory and per-process. A restart forgets every key,
including "outcome unknown" ones, and separate worker processes do not share it — run
the gateway as a single process. Capacity is per destination (scope), so one noisy
service cannot exhaust another's. Records that could cause a re-execution if forgotten
(in-flight, unknown) are never evicted for capacity; when only such records remain the
store refuses new keys for that destination (``full``) instead.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field

IDEMPOTENCY_TTL = 24 * 3600   # seconds a settled result stays replayable / blocking
IN_FLIGHT_TTL = 15 * 60       # an in-flight record older than this is treated as unknown
MAX_ENTRIES = 2_000  # per destination (scope)
MAX_KEY_LENGTH = 255
MAX_REPLAY_BODY = 256 * 1024  # larger results are remembered as "done" but not replayed

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
    body: bytes | None  # None = completed, but too large to replay
    media_type: str | None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _Record:
    fingerprint: str
    state: str
    updated_at: float
    response: StoredResponse | None = None


@dataclass(frozen=True)
class Decision:
    """``proceed`` | ``replay`` | ``mismatch`` | ``in_flight`` | ``unknown`` | ``full``."""

    kind: str
    response: StoredResponse | None = None


class IdempotencyStore:
    def __init__(self, ttl: float = IDEMPOTENCY_TTL, max_entries: int = MAX_ENTRIES,
                 in_flight_ttl: float = IN_FLIGHT_TTL):
        self._ttl = ttl
        self._max = max_entries
        self._in_flight_ttl = in_flight_ttl
        # Per scope (destination), ordered by last state change: settled records move to
        # the end, so the front holds the oldest ones.
        self._scopes: dict[str, OrderedDict[str, _Record]] = {}

    def _age_out(self, rec: _Record, now: float) -> bool:
        """Apply time-based transitions; True if the record has expired entirely."""
        if rec.state == IN_FLIGHT and now - rec.updated_at > self._in_flight_ttl:
            rec.state, rec.updated_at = UNKNOWN, now
        return rec.state != IN_FLIGHT and now - rec.updated_at > self._ttl

    def _records(self, scope: str) -> OrderedDict[str, _Record]:
        return self._scopes.setdefault(scope, OrderedDict())

    def _lookup(self, scope: str, key: str, now: float) -> _Record | None:
        records = self._scopes.get(scope)
        rec = records.get(key) if records else None
        if rec is not None and self._age_out(rec, now):
            del records[key]
            return None
        return rec

    @staticmethod
    def _classify(rec: _Record | None, fingerprint: str) -> Decision:
        if rec is None:
            return Decision("proceed")
        if rec.fingerprint != fingerprint:
            return Decision("mismatch")
        if rec.state == DONE:
            return Decision("replay", rec.response)
        return Decision(rec.state)  # in_flight | unknown

    def _make_room(self, scope: str, now: float) -> bool:
        records = self._records(scope)
        for k in [k for k, r in records.items() if self._age_out(r, now)]:
            del records[k]
        while len(records) >= self._max:
            victim = next((k for k, r in records.items() if r.state == DONE), None)
            if victim is None:
                return False  # only in-flight/unknown left: never evict those
            del records[victim]
        return True

    def peek(self, scope: str, key: str, fingerprint: str) -> Decision:
        """Non-mutating classification (used to replay before re-checking one-shot credentials)."""
        return self._classify(self._lookup(scope, key, time.monotonic()), fingerprint)

    def begin(self, scope: str, key: str, fingerprint: str) -> Decision:
        """Atomically classify the key; on ``proceed`` the key is marked in-flight.

        Must be called with no ``await`` between it and the caller acting on the result.
        """
        now = time.monotonic()
        decision = self._classify(self._lookup(scope, key, now), fingerprint)
        if decision.kind != "proceed":
            return decision
        if not self._make_room(scope, now):
            return Decision("full")
        self._records(scope)[key] = _Record(fingerprint=fingerprint, state=IN_FLIGHT, updated_at=now)
        return decision

    def _settle(self, scope: str, key: str, state: str, response: StoredResponse | None = None) -> None:
        records = self._scopes.get(scope)
        rec = records.get(key) if records else None
        if rec is not None:
            rec.state, rec.response, rec.updated_at = state, response, time.monotonic()
            records.move_to_end(key)

    def complete(self, scope: str, key: str, response: StoredResponse) -> None:
        self._settle(scope, key, DONE, response)

    def mark_unknown(self, scope: str, key: str) -> None:
        """The tool may or may not have run (timeout / dropped connection / failure after the call)."""
        self._settle(scope, key, UNKNOWN)

    def abandon(self, scope: str, key: str) -> None:
        """The tool definitely did not run — free the key for a retry."""
        records = self._scopes.get(scope)
        rec = records.get(key) if records else None
        if rec is not None and rec.state == IN_FLIGHT:
            del records[key]


def snapshot_response(status_code: int, body: bytes, media_type: str | None, headers) -> StoredResponse:
    kept = {k: v for k, v in headers.items() if k.lower().startswith(_REPLAY_HEADER_PREFIXES)}
    replayable = body if len(body) <= MAX_REPLAY_BODY else None
    return StoredResponse(status_code=status_code, body=replayable, media_type=media_type, headers=kept)


store = IdempotencyStore()
