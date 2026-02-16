"""Tier 1 + Tier 2 definer validation and bookend HMAC binding."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass


# Definer verb families — which verbs belong to which HTTP method
DEFINER_FAMILIES: dict[str, set[str]] = {
    "POST": {"CREATE", "SEND", "INVOKE", "TRIGGER", "UPLOAD"},
    "PUT": {"REPLACE", "OVERWRITE", "SET"},
    "PATCH": {"MODIFY", "APPEND", "AMEND", "RENAME"},
}

# Reverse lookup: verb → method family
VERB_TO_FAMILY: dict[str, str] = {}
for method, verbs in DEFINER_FAMILIES.items():
    for verb in verbs:
        VERB_TO_FAMILY[verb] = method

# Methods that require a definer
DEFINER_REQUIRED_METHODS = {"POST", "PUT", "PATCH"}


@dataclass
class DefinerResult:
    tier: int  # 1 or 2
    canonical: str  # the resolved definer verb (e.g. "INVOKE")
    bookend_hash: str | None = None  # the provided hash if Tier 2


@dataclass
class DefinerError:
    error_type: str  # "missing_definer", "definer_family_mismatch", "unknown_definer", "bookend_mismatch"
    detail: str
    status_code: int  # HTTP status code to return


def parse_definer_header(header_value: str | None) -> tuple[str | None, str | None]:
    """Parse X-Gimme-Definer header into (verb, bookend_hash_or_none)."""
    if not header_value:
        return None, None
    parts = header_value.strip().split(":", 1)
    verb = parts[0].strip().upper()
    bookend = parts[1].strip() if len(parts) > 1 else None
    return verb, bookend


def compute_bookend_hash(session_key: bytes, method: str, definer: str, payload: bytes) -> str:
    """Compute Tier 2 bookend HMAC hash.

    Format: truncate(HMAC-SHA256(session_key, METHOD:DEFINER:head(16):tail(16)), 4).hex()

    Edge cases per spec:
    - empty payload: head and tail are both empty bytes
    - payload < 16 bytes: head = tail = full payload
    - payload == 16 bytes: head = tail = full payload
    - payload > 16 bytes: head = first 16 bytes, tail = last 16 bytes
    """
    if len(payload) <= 16:
        head = payload
        tail = payload
    else:
        head = payload[:16]
        tail = payload[-16:]

    message = f"{method}:{definer}:".encode() + head + b":" + tail
    mac = hmac.new(session_key, message, hashlib.sha256).digest()
    return mac[:4].hex()


def validate_definer(
    method: str,
    header_value: str | None,
    payload: bytes,
    session_key: bytes,
) -> DefinerResult | DefinerError:
    """Validate the X-Gimme-Definer header for a request.

    Returns DefinerResult on success, DefinerError on failure.
    """
    method = method.upper()

    # Only POST/PUT/PATCH require definers
    if method not in DEFINER_REQUIRED_METHODS:
        return DefinerResult(tier=0, canonical="")

    # Missing header
    verb, bookend = parse_definer_header(header_value)
    if not verb:
        return DefinerError(
            error_type="missing_definer",
            detail=f"{method} requires X-Gimme-Definer header",
            status_code=400,
        )

    # Unknown verb
    if verb not in VERB_TO_FAMILY:
        return DefinerError(
            error_type="unknown_definer",
            detail=f"Unknown definer verb: {verb}. Valid verbs: {', '.join(sorted(VERB_TO_FAMILY.keys()))}",
            status_code=400,
        )

    # Family mismatch
    expected_family = VERB_TO_FAMILY[verb]
    if expected_family != method:
        return DefinerError(
            error_type="definer_family_mismatch",
            detail=f"{verb} belongs to {expected_family} family, not {method}",
            status_code=400,
        )

    # Tier 1 validated — check for Tier 2
    if bookend is None:
        return DefinerResult(tier=1, canonical=verb)

    # Tier 2: verify bookend HMAC
    expected = compute_bookend_hash(session_key, method, verb, payload)
    if not hmac.compare_digest(bookend.lower(), expected.lower()):
        return DefinerError(
            error_type="bookend_mismatch",
            detail="Tier 2 bookend hash does not match payload",
            status_code=403,
        )

    return DefinerResult(tier=2, canonical=verb, bookend_hash=bookend)
