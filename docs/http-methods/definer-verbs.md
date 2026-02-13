# Definer Verbs & Payload Binding

> **Status:** Draft
> **Extends:** [METHOD Tokenization](method-tokenization.md)
> **Applies to:** `PUT`, `POST`, `PATCH` only
> **Verb Atlas:** [verb-atlas.yaml](verb-atlas.yaml) (302 verbs, 12 canonicals, 3 families)
> **Authors:** Preston Richey, Claude (Anthropic)
> **Date:** 2026-02-05

---

## Motivation

[METHOD Tokenization](method-tokenization.md) eliminates verb/noun confusion by enforcing exactly one raw HTTP METHOD per request and tokenizing verb-like content in payloads. This is sufficient to prevent direct prompt injection through verb smuggling.

However, `PUT`, `POST`, and `PATCH` carry meaningful request bodies — and the payload itself is the remaining attack surface. An attacker need not smuggle a verb if they can manipulate the *content* that a legitimate verb acts on. The single METHOD is authenticated, but it is not **bound** to the specific payload it accompanies.

Definer Verbs address three residual risks:

| Risk | Description |
|------|-------------|
| **Intent ambiguity** | `POST` alone does not distinguish between "create," "send," "invoke," and "trigger" |
| **Payload tampering** | Injection at payload boundaries (prepend/append) goes undetected by METHOD tokenization |
| **Verb replay** | A captured request header can be replayed with a different payload |

---

## Overview

Three independently adoptable tiers, each adding a layer of protection:

| Tier | Enhancement | Cost | Protects Against |
|------|-------------|------|------------------|
| **Tier 1** | Static Definer | One header, one lookup | Intent ambiguity, verb-pair disagreement |
| **Tier 2** | Bookend Binding | One HMAC (32 bytes read) | Payload boundary tampering |
| **Tier 3** | POS Rotation | One embedding lookup + matrix rotation | Replay, verb prediction, interception |

Tiers compose additively. A Tier 3 implementation includes Tier 1 and Tier 2 by construction.

---

## Tier 1: Static Definer

### Concept

State-mutating HTTP methods (`POST`, `PUT`, `PATCH`) require a secondary **definer verb** that narrows the method's intent. The definer is transmitted in the `X-Gimme-Definer` request header.

### Definer Families

Each HTTP method has an associated family of valid definers:

```yaml
POST:
  canonical: CREATE
  family:
    - CREATE    # bring into existence
    - SEND      # transmit to recipient
    - INVOKE    # call a function/webhook
    - TRIGGER   # initiate a process
    - UPLOAD    # transfer a file

PUT:
  canonical: REPLACE
  family:
    - REPLACE   # full resource replacement
    - OVERWRITE # destructive replacement
    - SET       # assign a value

PATCH:
  canonical: MODIFY
  family:
    - MODIFY    # partial update
    - APPEND    # add to existing
    - AMEND     # correct/revise
    - RENAME    # change identifier
```

### Header Format

```http
POST /channels/C123/messages HTTP/2
Host: slack.gimme.tools
Authorization: Bearer <token>
X-Gimme-Definer: SEND
Content-Type: application/json

{"text": "Hello, world"}
```

### Validation

```python
DEFINER_FAMILIES = {
    'POST':  {'CREATE', 'SEND', 'INVOKE', 'TRIGGER', 'UPLOAD'},
    'PUT':   {'REPLACE', 'OVERWRITE', 'SET'},
    'PATCH': {'MODIFY', 'APPEND', 'AMEND', 'RENAME'},
}

def validate_definer(method, definer):
    """Tier 1: Verify definer belongs to the method's family."""
    if method not in DEFINER_FAMILIES:
        return True  # GET, DELETE, HEAD, OPTIONS: no definer required
    if definer is None:
        return False  # State-mutating methods MUST include a definer
    return definer in DEFINER_FAMILIES[method]
```

### Security Properties

- **Intent narrowing:** `POST` + `SEND` is more specific than `POST` alone. Permission scopes can optionally match on definers (e.g., `POST[SEND]:slack.gimme.tools/message.*`).
- **Verb-pair agreement:** A `DELETE`-class definer with a `POST` method is a mismatch — rejected before the payload is even read.
- **Cheap to implement:** One header, one set membership check. No cryptography required.

### When Definer is Absent

| Method | X-Gimme-Definer | Result |
|--------|-----------------|--------|
| `POST` | `SEND` | Valid |
| `POST` | `REPLACE` | Rejected — `REPLACE` is in the `PUT` family |
| `POST` | (missing) | Rejected — definer required for state-mutating methods |
| `GET` | (missing) | Valid — definer not required for safe methods |
| `GET` | `SEND` | Ignored — definer is meaningless for safe methods |

---

## Tier 2: Bookend Binding

### Concept

The definer token incorporates a fast checksum derived from the **first and last bytes** of the request payload. This binds the definer to the specific payload it accompanies, making boundary tampering detectable without hashing the full body.

### Token Format

```
X-Gimme-Definer: {DEFINER}:{BOOKEND_HASH}
```

Where:

```
BOOKEND_HASH = truncate(
    HMAC-SHA256(
        key  = session_key,
        data = METHOD + ":" + DEFINER + ":" + head(payload, 16) + ":" + tail(payload, 16)
    ),
    bytes = 4
)
```

Output: 8 hex characters.

### Example

```http
POST /channels/C123/messages HTTP/2
Host: slack.gimme.tools
X-Gimme-Definer: SEND:a7f3e91b

{"text": "Hello, world"}
```

Derivation:

```python
payload = b'{"text": "Hello, world"}'

hmac_input = b"POST:SEND:" + payload[:16] + b":" + payload[-16:]
#          = b"POST:SEND:{\"text\": \"Hello:" + b"Hello, world\"}"

bookend = HMAC_SHA256(session_key, hmac_input)[:4].hex()
# => "a7f3e91b"
```

### Validation

```python
def validate_bookend(method, definer, bookend_hash, payload, session_key):
    """Tier 2: Verify payload bookend binding."""
    head = payload[:16]
    tail = payload[-16:]
    # For payloads shorter than 32 bytes, head and tail may overlap — that's fine

    hmac_input = f"{method}:{definer}:".encode() + head + b":" + tail
    expected = HMAC_SHA256(session_key, hmac_input)[:4].hex()

    return constant_time_compare(bookend_hash, expected)
```

### Why Bookends, Not Full Hash

| Approach | Reads | Latency | Catches |
|----------|-------|---------|---------|
| Full payload HMAC | Entire body | High for large payloads | Everything |
| Bookend HMAC | 32 bytes | Near-zero | Prepend/append injection, truncation, boundary manipulation |
| No binding | 0 bytes | Zero | Nothing |

Prompt injection overwhelmingly operates at **payload boundaries** — prepending instructions before legitimate content, or appending them after. Bookend binding catches these at 32 bytes of read cost. Interior injection (modifying the middle of a payload without changing the first or last 16 bytes) is a narrower and harder attack that can be addressed by optional full-payload signing where required.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Empty payload | `head = tail = b""` — HMAC still computed, still valid |
| Payload < 16 bytes | `head = tail = full payload` — overlap is fine |
| Payload = 16 bytes | `head = tail = full payload` — equivalent to full hash |
| Streaming/chunked body | Bookend computed over the **first chunk's first 16 bytes** and **last chunk's last 16 bytes**. Requires buffering only 32 bytes total. |
| Multipart form data | Bookend computed over the raw multipart payload, including boundaries |

---

## Tier 3: POS-Rotated Verbs

### Concept

Instead of transmitting the definer as a static, predictable string, **rotate** it through a semantically-equivalent embedding neighborhood using the request nonce as the rotation key. Each request carries a different surface-form verb that maps back to the same canonical definer — functioning as a **one-time pad for verbs**.

### Prerequisites

- Tier 1 and Tier 2 (definer + bookend)
- A shared POS (Part-of-Speech) embedding model between client and server
- A monotonic request nonce (or timestamp + counter)

### POS Embedding Model

The rotation operates in a **verb-constrained** embedding space. Using Part-of-Speech embeddings (rather than general-purpose embeddings) ensures:

1. **Rotation stays in verb-space** — you cannot accidentally rotate `SEND` into `BANANA`
2. **Semantic neighborhood is tight** — valid rotations of `SEND` include `DISPATCH`, `TRANSMIT`, `RELAY`, `FORWARD`, not `CREATE` or `DELETE`
3. **Leverages existing infrastructure** — WebSpec already uses embeddings for NLP-driven resolution

#### Recommended Models

| Model | Dimensions | POS-Aware | Notes |
|-------|-----------|-----------|-------|
| spaCy `en_core_web_lg` | 300 | Yes (POS tags) | Good baseline, fast |
| Custom fine-tuned | 128-256 | Yes (verb-only) | Smallest, fastest, recommended for production |
| WebSpec verb atlas | 64 | Yes (curated) | Pre-computed for all definer families; see below |

#### The Verb Atlas (Recommended)

Rather than relying on a general embedding model, WebSpec ships a **curated verb atlas** — a pre-computed vocabulary covering the definer families and their semantic neighborhoods. The full atlas is defined in [`verb-atlas.yaml`](verb-atlas.yaml).

**Atlas summary (v1.0.0):**

| Family | Canonicals | Total Verbs | Semantic Range |
|--------|-----------|-------------|----------------|
| POST | CREATE, SEND, INVOKE, TRIGGER, UPLOAD | 125 | Creation, transmission, execution, initiation, ingestion |
| PUT | REPLACE, OVERWRITE, SET | 78 | Substitution, destruction, assignment |
| PATCH | MODIFY, APPEND, AMEND, RENAME | 99 | Alteration, extension, correction, re-identification |

**Invariants** (enforced at atlas-load time):
- No verb appears in more than one family
- No verb appears as a rotation candidate for more than one canonical within a family
- Every canonical is also a valid definer (canonicals are the Tier 1 vocabulary)

Each verb in the atlas will have a pre-computed embedding vector. At query time, the rotation algorithm selects from the canonical's rotation candidates based on the nonce-derived rotation. Negative examples per canonical document family boundaries and serve as repulsion vectors in the embedding space.

The atlas is versioned and shipped with the WebSpec client library / gateway. Both client and server reference the same atlas version.

### Rotation Algorithm

```python
import numpy as np
import hmac, hashlib

def rotate_definer(canonical: str, nonce: int, session_key: bytes,
                   atlas: dict, family: str) -> str:
    """
    Rotate a canonical definer verb through POS embedding space.

    Returns a semantically-equivalent verb that changes per-request.
    """
    # 1. Get the canonical verb's embedding from the atlas
    base_vector = np.array(atlas[family][canonical])

    # 2. Derive a deterministic rotation seed from nonce + session key
    seed = hmac.new(
        session_key,
        f"{nonce}:{canonical}".encode(),
        hashlib.sha256
    ).digest()

    # 3. Generate a rotation matrix from the seed
    #    (Gram-Schmidt orthogonalization of seed-derived random vectors)
    rotation_matrix = seed_to_rotation_matrix(seed, dims=base_vector.shape[0])

    # 4. Rotate the base vector
    rotated = rotation_matrix @ base_vector

    # 5. Find the nearest verb in the same family
    best_verb = None
    best_sim = -1.0
    for verb, embedding in atlas[family].items():
        if verb == canonical:
            continue  # Prefer a different surface form
        sim = cosine_similarity(rotated, np.array(embedding))
        if sim > best_sim:
            best_sim = sim
            best_verb = verb

    # 6. Verify the rotation stayed in-family (similarity threshold)
    assert best_sim > 0.70, "Rotation drifted out of family"

    return best_verb


def seed_to_rotation_matrix(seed: bytes, dims: int) -> np.ndarray:
    """
    Generate a deterministic rotation matrix from a seed.
    Uses seed bytes to initialize random vectors, then
    Gram-Schmidt orthogonalizes to produce a valid rotation.
    """
    rng = np.random.Generator(np.random.PCG64(int.from_bytes(seed[:16])))

    # Generate random matrix
    random_matrix = rng.standard_normal((dims, dims))

    # QR decomposition gives orthogonal Q (a rotation/reflection)
    Q, R = np.linalg.qr(random_matrix)

    # Ensure proper rotation (det = +1, not reflection)
    Q *= np.sign(np.diag(R))

    return Q
```

### Header Format (Tier 3)

```
X-Gimme-Definer: {ROTATED_VERB}:{BOOKEND_HASH}
X-Gimme-Nonce: {NONCE}
```

Example:

```http
POST /channels/C123/messages HTTP/2
Host: slack.gimme.tools
X-Gimme-Definer: DISPATCH:a7f3e91b
X-Gimme-Nonce: 1707148800001

{"text": "Hello, world"}
```

The canonical definer is `SEND`, but the surface form is `DISPATCH` for this specific request. The next request (nonce `...002`) might produce `TRANSMIT`.

### Server-Side Validation

```python
def validate_tier3(method, received_verb, bookend_hash, nonce,
                   payload, session_key, atlas):
    """
    Full Tier 3 validation: definer + bookend + rotation.
    """
    family = f"{method}_family"

    # 1. Is the received verb in the atlas for this method's family?
    if received_verb not in atlas[family]:
        return False, "Verb not in family atlas"

    # 2. Has this nonce been seen before? (replay protection)
    if nonce_is_used(nonce):
        return False, "Nonce replay"
    mark_nonce_used(nonce)

    # 3. Determine which canonical definer this is a rotation of.
    #    Try each canonical verb, rotate it with the given nonce,
    #    and check if the result matches the received verb.
    matched_canonical = None
    for canonical in DEFINER_FAMILIES[method]:
        expected_verb = rotate_definer(
            canonical, nonce, session_key, atlas, family
        )
        if expected_verb == received_verb:
            matched_canonical = canonical
            break

    if matched_canonical is None:
        return False, "Rotation mismatch — no canonical definer produces this verb"

    # 4. Validate bookend binding (Tier 2)
    #    Note: bookend HMAC uses the ROTATED verb, not the canonical,
    #    binding the specific surface form to the specific payload
    if not validate_bookend(method, received_verb, bookend_hash,
                            payload, session_key):
        return False, "Bookend mismatch"

    return True, matched_canonical
```

### Security Properties

| Property | Mechanism |
|----------|-----------|
| **Non-replayable** | Nonce is consumed on use; same nonce is rejected |
| **Non-predictable** | Next verb requires `HMAC(session_key, nonce)` — without the session key, the rotation is opaque |
| **Semantically constrained** | POS embeddings + family atlas ensure rotation stays within the correct verb class |
| **Tamper-evident** | Bookend hash includes the rotated verb — changing the verb or the payload breaks it |
| **Deterministic** | Both client and server compute the same rotation from the same inputs. No ML at validation time. |

### Why POS Rotation Is OTP-Like

A one-time pad has three properties:
1. The key is as long as the message
2. The key is used exactly once
3. The key is truly random

POS rotation approximates these:
1. The "key" (rotation matrix) operates over the full embedding dimensions
2. Each nonce produces a different rotation (used once)
3. The rotation is pseudo-random (HMAC-derived), computationally indistinguishable from random without the session key

It is not a true OTP (the verb vocabulary is finite, the rotation is pseudo-random), but it provides the **practical property** of OTPs: observing any number of past rotated verbs gives no advantage in predicting the next one.

### Verb Atlas Versioning

```yaml
# Atlas metadata
atlas_version: "1.0.0"
atlas_hash: "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
embedding_dims: 64
families: [POST_family, PUT_family, PATCH_family]
total_verbs: 87
```

Client and server must agree on atlas version. Version negotiation via the `X-Gimme-Atlas` header:

```http
X-Gimme-Atlas: 1.0.0
```

If the server does not support the client's atlas version, it responds `422 Unprocessable Entity` with an `X-Gimme-Atlas-Supported` header listing accepted versions.

---

## Complete Validation Pipeline

```python
def validate_definer_request(request, session_key, atlas):
    """
    Complete definer validation across all tiers.

    Returns (valid: bool, canonical_definer: str|None, tier: int, error: str|None)
    """
    method = request.method

    # Safe methods: no definer required
    if method in ('GET', 'DELETE', 'HEAD', 'OPTIONS'):
        return True, None, 0, None

    # Parse the definer header
    header = request.headers.get('X-Gimme-Definer')
    if header is None:
        return False, None, 0, "Missing X-Gimme-Definer for state-mutating method"

    parts = header.split(':')
    definer = parts[0]
    bookend = parts[1] if len(parts) > 1 else None
    nonce = request.headers.get('X-Gimme-Nonce')

    # ── Tier 1: Family membership ──
    if definer not in atlas.get(f"{method}_family", {}):
        # Check if it's a known verb in ANY family (cross-family attack)
        for fam in atlas.values():
            if definer in fam:
                return False, None, 1, f"Definer '{definer}' is in wrong family for {method}"
        return False, None, 1, f"Unknown definer verb: '{definer}'"

    # ── Tier 2: Bookend binding ──
    if bookend is not None:
        if not validate_bookend(method, definer, bookend, request.body, session_key):
            return False, None, 2, "Bookend hash mismatch"

        # ── Tier 3: POS rotation ──
        if nonce is not None:
            valid, canonical = validate_tier3(
                method, definer, bookend, int(nonce),
                request.body, session_key, atlas
            )
            if not valid:
                return False, None, 3, canonical  # canonical holds error message
            return True, canonical, 3, None

        # Tier 2 only: definer is taken at face value (must be canonical)
        if definer in DEFINER_FAMILIES[method]:
            return True, definer, 2, None
        return False, None, 2, "Non-canonical definer without rotation nonce"

    # Tier 1 only: static definer
    if definer in DEFINER_FAMILIES[method]:
        return True, definer, 1, None
    return False, None, 1, "Non-canonical definer without bookend binding"
```

---

## EBNF Grammar Extension

Extends the [Complete Grammar](../url-grammar/complete-grammar-ebnf.md):

```ebnf
(* Definer Verb Grammar — extends WebSpec request *)

definer_header  = definer_verb , [ ":" , bookend_hash ] ;
definer_verb    = UPPER_ALPHA , { UPPER_ALPHA } ;
bookend_hash    = 8 * HEXDIG ;

nonce_header    = DIGIT , { DIGIT } ;

(* Definer families — verb must match method *)
post_definer    = "CREATE" | "SEND" | "INVOKE" | "TRIGGER" | "UPLOAD" ;
put_definer     = "REPLACE" | "OVERWRITE" | "SET" ;
patch_definer   = "MODIFY" | "APPEND" | "AMEND" | "RENAME" ;

(* With Tier 3, definer_verb may be any verb in the method's atlas family *)
rotated_definer = atlas_verb ;    (* validated by rotation algorithm *)

(* Header declarations *)
"X-Gimme-Definer" = definer_header ;
"X-Gimme-Nonce"   = nonce_header ;
"X-Gimme-Atlas"   = semver ;

(* Terminals *)
UPPER_ALPHA     = "A" | "B" | ... | "Z" ;
HEXDIG          = "0" | ... | "9" | "a" | ... | "f" ;
DIGIT           = "0" | ... | "9" ;
semver          = DIGIT , { DIGIT } , "." , DIGIT , { DIGIT } , "." , DIGIT , { DIGIT } ;
```

---

## Interaction with Existing Spec

### Permission Scoping

Scope grammar can optionally incorporate the definer:

```
# Existing scope format
POST:slack.gimme.tools/message.*

# Extended scope format (optional — Tier 1+)
POST[SEND]:slack.gimme.tools/message.*
POST[CREATE]:slack.gimme.tools/channels.*
```

This allows tokens to be scoped not just to a method and path, but to a **specific intent**. A token with scope `POST[SEND]:slack.gimme.tools/message.*` cannot be used to `POST[INVOKE]` a webhook on the same path.

### METHOD Tokenization

Definer verbs in the `X-Gimme-Definer` header are **exempt** from tokenization — they are in a verb position (the header), not a data position (the payload). The METHOD tokenization invariant becomes:

> Exactly ONE raw HTTP METHOD in the request line. Exactly ONE definer verb in the `X-Gimme-Definer` header (for PUT/POST/PATCH). Zero untokenized METHOD-like strings in the payload.

### Token Structure

JWTs may include a `definers` claim restricting which definers a token may use:

```json
{
  "sub": "user-123",
  "aud": "slack.gimme.tools",
  "scope": ["POST:slack.gimme.tools/message.*"],
  "definers": {"POST": ["SEND"]},
  "exp": 1702603600
}
```

---

## Implementation Notes

### Client Libraries

Tier 1 is trivially implementable in any HTTP client — it is a single header.

Tier 2 requires HMAC-SHA256, available in every language's standard library.

Tier 3 requires:
- The verb atlas (shipped as a versioned JSON/YAML file)
- A NumPy-equivalent for matrix operations (or a pre-computed lookup table for small atlases)
- Nonce generation (monotonic counter or timestamp + counter)

For environments where matrix operations are impractical (embedded, shell scripts), a **simplified Tier 3** can use a pre-computed rotation table:

```yaml
# Pre-computed rotations for SEND at nonce % 8
SEND_rotations:
  0: DISPATCH
  1: TRANSMIT
  2: RELAY
  3: FORWARD
  4: CONVEY
  5: DELIVER
  6: ROUTE
  7: EMIT
```

The nonce modulo table-size selects the rotation. Less secure than full POS rotation (only 8 possible values) but still non-static and trivial to implement. The HMAC bookend (Tier 2) provides the primary tamper protection; the rotation adds unpredictability.

### Performance

| Tier | Added Latency | Added Bandwidth |
|------|--------------|-----------------|
| Tier 1 | ~0 (one set lookup) | ~20 bytes (header) |
| Tier 2 | ~1 microsecond (HMAC over 32 bytes) | ~30 bytes (header with hash) |
| Tier 3 | ~50 microseconds (matrix multiply + nearest-neighbor over ~30 verbs) | ~50 bytes (header + nonce) |

---

## Open Questions

1. **Should DELETE require a definer?** DELETE is idempotent and typically has no payload, but definers like `REMOVE` / `REVOKE` / `ARCHIVE` could clarify intent. Currently excluded for simplicity.

2. **Atlas update cadence?** The verb atlas should be stable (verbs don't change often), but new service integrations may introduce domain-specific verbs. Versioning handles this, but frequent updates create client/server skew.

3. **Interaction with HTTP/3?** QUIC's multiplexed streams don't change the definer model (it operates at the request level), but nonce ordering across concurrent streams needs consideration.

4. **Should the bookend HMAC cover more than 16+16 bytes?** 32 bytes total catches boundary attacks. A configurable `X-Gimme-Bookend-Depth` header could allow clients to request deeper coverage at the cost of more I/O.
