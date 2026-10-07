# Status and roadmap

## Implementation

The reference gateway (`gateway/` in the
[repository](https://github.com/I-m-A-g-I-n-E/WebSpec)) implements the rules in this spec,
with the deviations listed under [Known limits](#known-limits). Its tests run in CI on Python
3.11, 3.12, and 3.13. They drive the real HTTP application, and an integration suite runs it against
real MCP servers over stdio, including one that crashes in the middle of a call.

| Area | State |
|---|---|
| Host routing, qualifier allow-lists (qualifiers fail closed) | **Implemented** |
| Tool contracts, pinning, method binding | **Implemented** |
| Guard, definers and bookend, `Idempotency-Key`, clearance, human approval | **Implemented** |
| Audit chain, process hardening | **Implemented** |
| Reference deployments: Linux system service and Caddy, macOS LaunchDaemon, Docker stack | **Implemented** |
| Everything under [Roadmap](#roadmap) | **Proposed** |

Some extension points for proposed work are marked in the code with a `TODO(C)` comment.

## Conformance

A gateway conforms at level *n* when it meets every rule that applies at levels up to *n*:

| Rules | Apply |
|---|---|
| Addressing: HG-1 to HG-7, PA-1 to PA-3, AR-1 to AR-5 | at every level |
| Contracts and binding: TC-1 to TC-6, MB-1 to MB-3, RQ-1, RQ-2 | at every level |
| Definers: DF-1, DF-3, and DF-2 whenever a bookend is sent | at every level |
| Responses and discovery: RS-1 to RS-4, DS-1 to DS-3 | at every level |
| Levels, processing, and commit: LV-1, PO-1, PO-2, CO-1, CO-2 | at every level |
| Audit: AU-1 to AU-4 | at every level |
| Guard: GD-1 to GD-5 | from level 1 |
| Required bookend (DF-2) and required `Idempotency-Key` (ID-1) | from level 2 |
| Idempotency behavior: ID-2 to ID-5 | whenever a key is sent, and always from level 2 |
| Clearance: CL-1 to CL-3 | from level 3 |
| Approval: AP-1 to AP-7 | at level 4 |
| Deployment: DP-1 to DP-9 | in every deployment |
| Harness: SH-1, SH-2 | on the client side |

## Known limits

These are true of the reference gateway today. Each one is a reason to choose a higher level
or to watch the roadmap.

- **One symmetric key.** The guard, the bookend, and the clearance are all keyed with the guard
  key, so whoever holds it can produce everything that levels 1 to 3 require. Only level 4
  rests on a separate, asymmetric key that a person holds.
- **Short tags, cheap guesses.** Guard, bookend, and clearance tags are 32 bits. A failed guard
  check does not consume its nonce. And the tag on `GET /__nonce` covers a constant message, so
  whoever sees it once can mint nonces for that host indefinitely. Online guessing is therefore
  limited only by request rate. The Linux reference Caddy (`setup-caddy.sh`) allows each client
  that comes through the tunnel 60 requests a minute per destination, and 120 for nonce
  requests, counting IPv6 clients per /64. The Docker stack's Caddy has no rate limit, and the
  macOS deployment ships no proxy, so there only the edge can limit the rate. Those limits bind clients of the tunnel only: a local process can send
  `Cf-Ray` and `Cf-Connecting-Ip` itself and pick its own count. Treat nonce-request tags as
  secrets, also rate-limit at the edge, and watch the roadmap.
- **Clearances have one-second resolution.** A clearance carries no nonce of its own. Two
  clearances minted in the same second for the identical call are therefore the same token,
  and the second is refused as `clearance_reused`. A harness that really means to repeat a call
  must wait a second.
- **The bookend adds nothing over the guard.** At the levels that require a bookend, the guard
  already signs the whole body and the definer header.
- **Memory-only state.** A restart forgets nonces, clearances, approvals, idempotency records
  (including "outcome unknown" ones), and contract pins. After a restart, the gateway trusts
  the first tool listing it sees again. Under the reference units, any stdio server can force
  such a restart, because it runs as the gateway's user.
- **Shape, not meaning.** Arguments are checked for form (strict JSON, JSON-decoded `GET`
  values, unique query keys) but are not validated against the tool's `inputSchema`, and
  duplicate keys inside a JSON body are not refused. Nothing checks that a call matches what the
  user asked for.
- **Open-world reads are not gated.** A read-only open-world tool (a fetcher, a search API) can
  carry data out in its arguments with neither a clearance nor an approval, at any level, unless
  it is raised to `dangerous` ([RQ-1](methods.md#requirements)).
- **Taint is declared, not observed.** `X-UFO-Taint` marks open-world output, but the policy
  that acts on it lives in the harness shim. The gateway does not itself track which data
  steered a call.
- **Destinations are listed.** `GET /` on the domain itself returns every destination and its
  level, without the guard. The reference proxies answer `421` for the bare domain, so it is
  reachable only from the host. The `404 unknown_service` answer lists the destinations only
  under loopback names.
- **Audit gaps.** Discovery, nonce issuance, and a few framework-level refusals are not audited,
  and removing the tail of the log is not detectable from the file alone
  ([AU-1, AU-3](audit-deployment.md#audit)). Under the reference units the stdio servers run
  as the gateway's user, so they can write the log: ship it, or at least its latest hash, off
  the host.
- **Idempotency is best effort under load.** When a destination's store is full, completed
  records are evicted oldest first, and a retry whose record was evicted runs again
  ([ID-5](levels.md#level-2-bound)).
- **A key is needed even at level 0.** The reference gateway needs a guard key
  (`WEBSPEC_GUARD_KEY` or `WEBSPEC_GUARD_KEY_FILE`) for every unsafe request at every level
  ([GD-5](levels.md#level-1-signed)).
- **What the reference deployments leave to the operator.** None of them sets up DP-3: the
  agent's egress is confined by the operator ([Deploying](../guide/deploy.md#egress-dp-3)).
  The macOS deployment ships no proxy, so DP-5 and DP-6 there rest on the operator's own, and
  DP-2 has no macOS equivalent. In the Docker stack, Docker lets Caddy's published port go
  whenever Caddy's container is down, so DP-9 holds there for the gateway's port only, and a
  process that holds the port when Docker starts Caddy again keeps it until it stops.
  Everywhere, the stdio MCP
  servers share the gateway's user:
  they can read what it reads (on macOS the guard-key file and every secret), read each other's
  environment, and signal the gateway. Run only stdio servers you trust as much as the gateway.
  `gateway/systemd` and `gateway/launchd` are development units that run the gateway as the
  logged-in user; they meet neither DP-1 nor DP-4.

## Roadmap

All proposed.

**Signing**

- Replace the guard with RFC 9421 HTTP Message Signatures, using an independent signer per
  level. The harness signs level 1, a policy service signs level 3, and a person signs level 4.
  The gateway would then hold only public keys.
- Widen all tags to at least 128 bits. Consume a nonce on a failed guard check, and give
  nonce requests a tag that expires.

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
  enforce them. Today the gateway ignores the header; a validator exists but is not called.
- Refuse duplicate keys in JSON bodies.

**Addressing**

- Per-qualifier backends, with edge support (Caddy host blocks and per-destination wildcard
  certificates).

**Audit and approval**

- Off-host audit shipping (or at least the latest hash), and failing closed at level 3 and
  above when the log cannot be written.
- Audit discovery and nonce issuance, and stop listing destinations to unauthenticated
  callers.
- An out-of-band approval device, such as a phone push that shows the summary.

**Registry** (`gateway/webspec_registry`)

- Semantic matching over multiple vectors.
- Recency and preference boosts.
- Per-provider quota and liveness enrichment.
- Linking accounts to services.
- Ordering by noun containment.
- Public exposure behind the guard.
- A single-flight catalog cache.

**Deployment**

- A proxy configuration for macOS.
- A proxy listener that only the tunnel can reach, so that Caddy tells tunneled requests from
  local ones by where they arrive, not by headers a local process can send.
- Stdio servers under users of their own, and a watchdog for a gateway that a signal stopped.
- Egress tooling for DP-3.

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
5. **Open-world reads.** Should a read-only open-world tool need a clearance at level 3 (and a
   person at level 4) by default, rather than only when it is raised to `dangerous`?

## Versions

- **2026-10.** Method profiles and levels 0–4. The spec was rewritten to match the gateway.
  Earlier documents, including the REST-hierarchy URL grammar, METHOD tokenization, and Tier-3
  rotated definers, are retired and remain in the git history. Later that month, reference
  deployments for Linux, macOS, and Docker were added, with DP-9 (held ports); stricter DP-3,
  DP-6, and DP-7; a DP-5 that admits loopback names from the host itself and refuses them from
  outside; the passed-socket rule in DP-8; audited guard-key refusals (AU-1); and the key file
  and start-up rules of GD-5.
