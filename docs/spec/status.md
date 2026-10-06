# Status and roadmap

## Implementation

The reference gateway (`gateway/` in the
[repository](https://github.com/I-m-A-g-I-n-E/WebSpec)) implements every rule in this spec.
Its tests run in CI on Python 3.11 and 3.12. They drive the real HTTP application, and an
integration suite runs it against a real MCP server over stdio.

| Area | State |
|---|---|
| Host routing, qualifier allow-lists (qualifiers fail closed) | **Implemented** |
| Tool contracts, pinning, method binding | **Implemented** |
| Guard, definers and bookend, `Idempotency-Key`, clearance, human approval | **Implemented** |
| Audit chain, process hardening | **Implemented** |
| Everything under [Roadmap](#roadmap) | **Proposed** |

Code marks each extension point for proposed work with a `TODO(C)` comment.

## Conformance

A gateway conforms at level *n* when it meets every rule that applies at levels up to *n*:

| Rules | Apply |
|---|---|
| Addressing: HG-1 to HG-7, PA-1 to PA-3, AR-1 to AR-5 | at every level |
| Contracts and binding: TC-1 to TC-6, MB-1 to MB-3, RQ-1, RQ-2 | at every level |
| Definers: DF-1, DF-3, and DF-2 whenever a bookend is sent | at every level |
| Responses and discovery: RS-1 to RS-4, DS-1 to DS-3 | at every level |
| Processing and commit: PO-1, PO-2, CO-1, CO-2 | at every level |
| Audit: AU-1 to AU-4 | at every level |
| Guard: GD-1 to GD-5 | from level 1 |
| Required bookend (DF-2) and required `Idempotency-Key` (ID-1) | from level 2 |
| Idempotency behavior: ID-2 to ID-5 | whenever a key is sent, and always from level 2 |
| Clearance: CL-1 to CL-3 | from level 3 |
| Approval: AP-1 to AP-7 | at level 4 |
| Deployment: DP-1 to DP-8 | in every deployment |
| Harness: SH-1, SH-2 | on the client side |

## Known limits

These are true of the reference gateway today. Each one is a reason to choose a higher level
or to watch the roadmap.

- **One symmetric key.** The guard, the bookend, and the clearance are all keyed with the guard
  key, so whoever holds it can produce everything that levels 1 to 3 require. Only level 4
  rests on a separate, asymmetric key that a person holds.
- **Short tags.** Guard, bookend, and clearance tags are 32 bits. The single-use nonce and the
  30–60 s lifetimes limit guessing online, but the tags should be wider.
- **Clearances have one-second resolution.** A clearance carries no nonce of its own. Two
  clearances minted in the same second for the identical call are therefore the same token,
  and the second is refused as `clearance_reused`. A harness that really means to repeat a call
  must wait a second.
- **The bookend adds nothing over the guard.** At the levels that require a bookend, the guard
  already signs the whole body and the definer header.
- **Memory-only state.** A restart forgets nonces, clearances, approvals, idempotency records
  (including "outcome unknown" ones), and contract pins. After a restart, the gateway trusts
  the first tool listing it sees again.
- **Shape, not meaning.** Arguments are checked for form (strict JSON, typed `GET` values,
  unique keys) but are not validated against the tool's full `inputSchema`. Nothing checks
  that a call matches what the user asked for.
- **Taint is declared, not observed.** `X-UFO-Taint` marks open-world output, but the policy
  that acts on it lives in the harness shim. The gateway does not itself track which data
  steered a call.
- **The bare domain lists destinations.** `GET /` on the domain itself returns every
  destination and its level.

## Roadmap

All proposed.

**Signing**

- Replace the guard with RFC 9421 HTTP Message Signatures, using an independent signer per
  level. The harness signs level 1, a policy service signs level 3, and a person signs level 4.
  The gateway would then hold only public keys.
- Widen all tags to at least 128 bits.

**State**

- A durable, crash-safe store (for example SQLite) for nonces, idempotency records, and
  approvals.
- Persistent contract pins backed by an operator-approved snapshot.

**Policy**

- Gateway-observed taint: the gateway records what open-world data it has served and refuses
  clearances that depend on it.
- An intent layer that checks a call against the user's stated task.
- Full `inputSchema` validation.
- Scoped tokens: `METHOD:host/path` patterns per caller, in place of today's allow-all default
  (each tool's `OPTIONS` already names its scopes).
- Proof of read: `If-Match` with a gateway-issued ETag on `PUT`, `PATCH`, and `DELETE`, so that
  an agent can only overwrite what it has seen.
- Provenance v2: bind `X-UFO-Provenance` links to the request fingerprint and nonce, then
  enforce them. Today they are parsed but not enforced.

**Addressing**

- Per-qualifier backends, with edge support (Caddy host blocks and per-destination wildcard
  certificates).

**Audit and approval**

- Off-host audit shipping, and failing closed at level 3 and above when the log cannot be
  written.
- An out-of-band approval device, such as a phone push that shows the summary.

**Registry** (`gateway/webspec_registry`)

- Semantic matching over multiple vectors.
- Recency and preference boosts.
- Per-provider quota and liveness enrichment.
- Linking accounts to services.
- Ordering by noun containment.
- Public exposure behind the guard.
- A single-flight catalog cache.

**Conformance**

- A conformance suite and a vulnerability scanner that test each rule by its ID.

The long-range vision, a hosted, multi-tenant WebSpec with OAuth identity, keychain discovery,
service registration, and REST collection paths, is kept outside this spec until it is built.

## Open questions

1. **Path word order.** The path should read as *what it is, what it does, and what it is done
   to*. The canonical order of those words is not fixed. Today the path is the MCP tool name.
2. **Retire the bookend?** It is subsumed by the guard at every level that requires it.
3. **Clearance key.** Until independent signers exist, should level 3 at least use a key
   separate from the guard key?
4. **Public index.** Should the bare domain stop listing destinations?

## Versions

- **2026-10.** Method profiles and levels 0–4. The spec was rewritten to match the gateway.
  Earlier documents, including the REST-hierarchy URL grammar, METHOD tokenization, and Tier-3
  rotated definers, are retired and remain in the git history.
