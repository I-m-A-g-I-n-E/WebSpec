"""Guard middleware: session-key HMAC authentication + audience-bound single-use nonces."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlencode

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


def canonical_json(obj) -> str:
    """The one canonical JSON form used for every MAC, fingerprint, and audit hash."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def loads_strict(text: str | bytes):
    """json.loads that refuses NaN, ±Infinity and overflowing numbers (e.g. 1e400).

    Such values can't be re-serialized as JSON, so accepting them lets a request (or a
    tool result) crash the gateway after side effects have happened. Raises ValueError.
    """
    def _const(name: str):
        raise ValueError(f"non-finite JSON constant {name}")

    def _float(text: str) -> float:
        value = float(text)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"non-finite JSON number {text}")
        return value

    return json.loads(text, parse_constant=_const, parse_float=_float)


def duplicate_query_keys(raw_query: str) -> list[str]:
    """Keys that appear more than once. WebSpec requests MUST NOT repeat a query key:
    the tool would see only one value, so a signature over all of them would not bind
    the argument actually used."""
    seen, dupes = set(), []
    for key, _ in parse_qsl(raw_query or "", keep_blank_values=True):
        if key in seen and key not in dupes:
            dupes.append(key)
        seen.add(key)
    return dupes


def canonical_query(raw_query: str) -> str:
    """Canonical query string for signing: decoded pairs sorted by key, re-encoded RFC 3986.

    Blank values are kept. Keys are unique (requests with repeated keys are refused
    before signing is checked). Clients MUST sign this exact form (spec: method-profiles.md).
    """
    if not raw_query:
        return ""
    pairs = sorted(parse_qsl(raw_query, keep_blank_values=True))
    return urlencode(pairs, quote_via=quote, safe="-._~")


def compute_guard_hmac(
    session_key: bytes,
    method: str,
    host: str,
    path: str,
    nonce: str,
    body: bytes,
    query: str = "",
    definer: str = "",
    idempotency_key: str = "",
) -> str:
    """Compute guard HMAC → 8 hex chars.

    HMAC-SHA256(key, METHOD:host:path:nonce:sha256(body)[:?query][:!definer][:#idempotency-key]),
    truncated. Each suffix is present only when that part is present, so a request with
    none of them signs exactly as before. ``query`` must already be canonical; ``definer``
    and ``idempotency_key`` are the raw X-Gimme-Definer / Idempotency-Key header values.
    """
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{method}:{host}:{path}:{nonce}:{body_hash}"
    if query:
        message += f":?{query}"
    if definer:
        message += f":!{definer}"
    if idempotency_key:
        message += f":#{idempotency_key}"
    message = message.encode()
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
    query: str = "",
    audience: str | None = None,
    definer: str = "",
    idempotency_key: str = "",
) -> GuardResult | GuardError:
    """Validate guard headers on a request.

    For /__nonce requests: only HMAC required (no nonce — this is bootstrap).
    For all other requests: HMAC + valid nonce required.
    """
    if not guard_header:
        return GuardError("guard_missing", "X-WebSpec-Guard header required", 401)

    # For nonce requests, nonce field in HMAC is empty string
    nonce_value = "" if is_nonce_request else (nonce_header or "")

    expected = compute_guard_hmac(session_key, method, host, path, nonce_value, body, canonical_query(query),
                                  definer=definer, idempotency_key=idempotency_key)
    if not hmac.compare_digest(guard_header.lower(), expected.lower()):
        return GuardError("guard_invalid", "HMAC verification failed", 403)

    # Nonce requests don't need nonce validation
    if is_nonce_request:
        return GuardResult()

    # All other requests need a valid nonce
    if not nonce_header:
        return GuardError("nonce_missing", "X-WebSpec-Nonce header required", 401)

    # Audience = the destination service label. Callers pass it explicitly because with
    # qualifier labels (eu.slack.<domain>) the leftmost label is not the destination.
    if audience is None:
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
    return canonical_json(args)


def compute_clearance_token(
    session_key: bytes, tool: str, args: dict, timestamp: str, *, service: str = "", method: str = ""
) -> str:
    """UFO clearance token → 8 hex chars.

    HMAC-SHA256(key, ufo2:service:METHOD:tool:canonical_args:ts). Bound to the destination
    and method as well as (tool, args), so a token minted for one service cannot be spent
    on another service that happens to expose a tool of the same name.
    """
    canonical = _canonical_args(args)
    message = f"ufo2:{service}:{method.upper()}:{tool}:{canonical}:{timestamp}".encode()
    mac = hmac.new(session_key, message, hashlib.sha256).digest()
    return mac[:4].hex()


class _SpentClearances:
    """Clearance tokens are single-use: remember spent ones until they would expire anyway."""

    def __init__(self) -> None:
        self._spent: dict[str, float] = {}

    def _purge(self, now: float) -> None:
        for k in [k for k, exp in self._spent.items() if exp < now]:
            del self._spent[k]

    def is_spent(self, token_and_ts: str) -> bool:
        self._purge(time.monotonic())
        return token_and_ts in self._spent

    def spend(self, token_and_ts: str) -> bool:
        """Mark as spent; False if it already was."""
        now = time.monotonic()
        self._purge(now)
        if token_and_ts in self._spent:
            return False
        self._spent[token_and_ts] = now + 2 * CLEARANCE_TTL + 1
        return True


spent_clearances = _SpentClearances()


def _clearance_key(header: str, service: str) -> str:
    token, _, timestamp = header.rpartition(":")
    return f"{token.lower()}:{timestamp}:{service}"


def clearance_spent(header: str, service: str = "") -> bool:
    return spent_clearances.is_spent(_clearance_key(header, service))


def spend_clearance(header: str, service: str = "") -> bool:
    """Spend a clearance that already validated. False if it was already spent.

    Callers validate early (``spend=False``) and spend only once the request is
    committed to run, so a request refused for some later reason (e.g. a level-4
    approval challenge) does not burn the token its approved retry needs.
    """
    return spent_clearances.spend(_clearance_key(header, service))


def validate_clearance_token(
    session_key: bytes, tool: str, args: dict, header: str | None, *, service: str = "", method: str = "",
    spend: bool = True,
) -> str | None:
    """Validate X-UFO-Clearance header (and, by default, spend it). Returns None on success, else an error."""
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
    expected = compute_clearance_token(session_key, tool, args, timestamp, service=service, method=method)
    if not hmac.compare_digest(token.lower(), expected.lower()):
        return "clearance_invalid"

    if spend and not spend_clearance(header, service):
        return "clearance_reused"
    return None


@dataclass
class ProvenanceResult:
    valid: bool
    origin: str  # "human", "agent", "unknown"
    links: list[str] = field(default_factory=list)
    error: str | None = None


def build_provenance_link(session_key: bytes, source: str, target: str, action: str) -> str:
    """Build a provenance link signature: HMAC(key, source->target:action) -> 4 hex chars."""
    message = f"{source}->{target}:{action}".encode()
    mac = hmac.new(session_key, message, hashlib.sha256).digest()
    return mac[:4].hex()


def validate_provenance_chain(
    session_key: bytes, chain_header: str | None, action: str
) -> ProvenanceResult:
    """Validate X-UFO-Provenance header. Walks the chain and verifies each link."""
    if not chain_header:
        return ProvenanceResult(valid=False, origin="unknown", error="provenance_missing")

    # Parse: "human:h3a9->agent:a7f2->gateway"
    links = chain_header.split("->")
    if not links:
        return ProvenanceResult(valid=False, origin="unknown", error="provenance_empty")

    parsed = []
    for link in links:
        parts = link.split(":", 1)
        if len(parts) != 2:
            return ProvenanceResult(valid=False, origin="unknown", error="provenance_malformed")
        parsed.append((parts[0], parts[1]))  # (role, signature)

    # Verify each link's signature
    _implicit_next: dict[str, str] = {"human": "agent", "agent": "gateway"}
    for i, (role, sig) in enumerate(parsed):
        if i + 1 < len(parsed):
            target_role = parsed[i + 1][0]
        else:
            target_role = _implicit_next.get(role, "gateway")  # infer next hop by role
        expected = build_provenance_link(session_key, role, target_role, action)
        if not hmac.compare_digest(sig.lower(), expected.lower()):
            return ProvenanceResult(
                valid=False, origin=parsed[0][0],
                links=[p[0] for p in parsed], error="provenance_invalid"
            )

    return ProvenanceResult(
        valid=True, origin=parsed[0][0], links=[p[0] for p in parsed]
    )
