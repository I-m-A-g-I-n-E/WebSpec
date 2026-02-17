"""Guard middleware: session-key HMAC authentication + audience-bound single-use nonces."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

NONCE_TTL = 60  # seconds
NONCE_CLEANUP_INTERVAL = 30  # seconds


@dataclass
class Nonce:
    value: str
    audience: str  # service subdomain this nonce is bound to
    created_at: float
    used: bool = False


class NonceStore:
    """In-memory nonce store with periodic cleanup."""

    def __init__(self, ttl: float = NONCE_TTL):
        self._nonces: dict[str, Nonce] = {}
        self._ttl = ttl
        self._last_cleanup = time.monotonic()

    def create(self, audience: str) -> Nonce:
        """Generate a new nonce bound to the given audience (service subdomain)."""
        self._maybe_cleanup()
        value = secrets.token_hex(16)
        nonce = Nonce(value=value, audience=audience, created_at=time.monotonic())
        self._nonces[value] = nonce
        return nonce

    def consume(self, value: str, audience: str) -> str | None:
        """Validate and consume a nonce. Returns None on success, error string on failure."""
        self._maybe_cleanup()
        nonce = self._nonces.get(value)
        if nonce is None:
            return "unknown_nonce"
        if nonce.used:
            return "nonce_reused"
        if time.monotonic() - nonce.created_at > self._ttl:
            return "nonce_expired"
        if nonce.audience != audience:
            return "nonce_audience_mismatch"
        nonce.used = True
        return None

    def _maybe_cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < NONCE_CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        cutoff = now - self._ttl
        expired = [k for k, n in self._nonces.items() if n.created_at < cutoff]
        for k in expired:
            del self._nonces[k]


def compute_guard_hmac(
    session_key: bytes,
    method: str,
    host: str,
    path: str,
    nonce: str,
    body: bytes,
) -> str:
    """Compute guard HMAC: truncated HMAC-SHA256(key, METHOD:host:path:nonce:sha256(body)) → 8 hex chars."""
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{method}:{host}:{path}:{nonce}:{body_hash}".encode()
    mac = hmac.new(session_key, message, hashlib.sha256).digest()
    return mac[:4].hex()  # 8 hex chars


@dataclass
class GuardResult:
    """Successful guard validation."""
    pass


@dataclass
class GuardError:
    error_type: str
    detail: str
    status_code: int


# Module-level nonce store (singleton)
nonce_store = NonceStore()


def validate_guard(
    session_key: bytes,
    method: str,
    host: str,
    path: str,
    body: bytes,
    guard_header: str | None,
    nonce_header: str | None,
    is_nonce_request: bool = False,
) -> GuardResult | GuardError:
    """Validate guard headers on a request.

    For /__nonce requests: only HMAC required (no nonce — this is bootstrap).
    For all other requests: HMAC + valid nonce required.
    """
    if not guard_header:
        return GuardError("guard_missing", "X-WebSpec-Guard header required", 401)

    # For nonce requests, nonce field in HMAC is empty string
    nonce_value = "" if is_nonce_request else (nonce_header or "")

    expected = compute_guard_hmac(session_key, method, host, path, nonce_value, body)
    if not hmac.compare_digest(guard_header.lower(), expected.lower()):
        return GuardError("guard_invalid", "HMAC verification failed", 403)

    # Nonce requests don't need nonce validation
    if is_nonce_request:
        return GuardResult()

    # All other requests need a valid nonce
    if not nonce_header:
        return GuardError("nonce_missing", "X-WebSpec-Nonce header required", 401)

    # Extract audience from host (service subdomain)
    audience = host.split(".")[0]
    error = nonce_store.consume(nonce_header, audience)
    if error:
        detail_map = {
            "unknown_nonce": "Nonce not recognized",
            "nonce_reused": "Nonce already used (single-use)",
            "nonce_expired": f"Nonce expired (TTL {NONCE_TTL}s)",
            "nonce_audience_mismatch": "Nonce bound to a different service",
        }
        return GuardError(error, detail_map.get(error, error), 403)

    return GuardResult()


def generate_nonce(audience: str) -> dict:
    """Generate a new nonce for the given audience. Returns JSON-serializable dict."""
    nonce = nonce_store.create(audience)
    return {
        "nonce": nonce.value,
        "audience": nonce.audience,
        "expires_at": nonce.created_at + NONCE_TTL,
        "ttl_seconds": NONCE_TTL,
    }


CLEARANCE_TTL = 30  # seconds


def _canonical_args(args: dict) -> str:
    """Canonicalize tool arguments: sorted keys, JSON-serialized."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"))


def compute_clearance_token(session_key: bytes, tool: str, args: dict, timestamp: str) -> str:
    """Compute UFO clearance token: HMAC-SHA256(key, ufo:tool:canonical_args:ts) -> 8 hex chars."""
    canonical = _canonical_args(args)
    message = f"ufo:{tool}:{canonical}:{timestamp}".encode()
    mac = hmac.new(session_key, message, hashlib.sha256).digest()
    return mac[:4].hex()


def validate_clearance_token(
    session_key: bytes, tool: str, args: dict, header: str | None
) -> str | None:
    """Validate X-UFO-Clearance header. Returns None on success, error string on failure."""
    if not header:
        return "clearance_missing"

    parts = header.rsplit(":", 1)
    if len(parts) != 2:
        return "clearance_malformed"

    token, timestamp = parts
    if len(token) != 8:
        return "clearance_malformed"

    # Check expiry
    try:
        ts_int = int(timestamp)
    except ValueError:
        return "clearance_malformed"
    if abs(time.time() - ts_int) > CLEARANCE_TTL:
        return "clearance_expired"

    # Verify HMAC
    expected = compute_clearance_token(session_key, tool, args, timestamp)
    if not hmac.compare_digest(token.lower(), expected.lower()):
        return "clearance_invalid"

    return None
