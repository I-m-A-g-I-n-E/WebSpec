# Status and roadmap

## Implementation

The reference gateway (`gateway/` in the
[repository](https://github.com/I-m-A-g-I-n-E/WebSpec)) implements the rules in this spec.
[Known limits](#known-limits) says where it, or the spec itself, falls short. Its tests run in CI on Python
3.11, 3.12, and 3.13. They drive the real HTTP application, and an integration suite runs it against
real MCP servers over stdio, including one that crashes in the middle of a call.

| Area | State |
|---|---|
| [Host routing](addressing.md#host), qualifier allow-lists (qualifiers fail closed) | **Implemented** |
| [Tool contracts, pinning, method binding](methods.md) | **Implemented** |
| [Guard](levels.md#level-1-signed), [definers and bookend](methods.md#per-method-rules), [`Idempotency-Key`](levels.md#level-2-bound), [clearance](levels.md#level-3-cleared), [human approval](levels.md#level-4-witnessed) | **Implemented** |
| [Audit chain](audit-deployment.md#audit), non-dumpable process on Linux ([DP-2](audit-deployment.md#deployment)) | **Implemented** |
| [Reference deployments](../guide/deploy.md): Linux system service and Caddy, macOS LaunchDaemon, Docker stack | **Implemented** |
| Everything under [Roadmap](#roadmap) | **Proposed** |

## Known limits

These are true of the reference gateway today. Some come from the rules themselves, such as
32-bit tags ([GD-2](levels.md#level-1-signed)), and so hold for any conforming gateway.

- **No check of intent.** Nothing checks that a call matches what the user asked for. At
  level 4 a person does, for the calls that need approval ([RQ-1](methods.md#requirements)).
- **One symmetric key.** The guard, the bookend, and the clearance are all keyed with the guard
  key, so whoever holds it can produce everything that levels 1 to 3 require. Only level 4
  rests on a separate, asymmetric key that a person holds.
- **Short tags, cheap guesses.** Guard, bookend, and clearance tags are 32 bits. A failed guard
  check does not consume its nonce, and the tag on `GET /__nonce` signs the same message every
  time, so whoever sees it once can get nonces for that host indefinitely. Online guessing is
  therefore limited only by request rate. The Linux reference Caddy (`setup-caddy.sh`) allows
  each client that comes through the tunnel 60 requests a minute per destination, and 120 for
  nonce requests, counting IPv6 clients per /64. A local process, the agent included, escapes
  that count: it can send Cloudflare's `Cf-Ray` and `Cf-Connecting-Ip` headers itself and pick
  the client it is counted as. The Docker stack's Caddy has no rate limit, and the macOS
  deployment ships no proxy. Treat nonce-request tags as secrets, and rate-limit at the edge.
- **Clearances have one-second resolution.** A clearance carries no nonce of its own. Two
  clearances minted in the same second for the identical call are therefore the same token,
  and the second is refused as `clearance_reused`. A harness that really means to repeat a call
  must wait a second.
- **The bookend adds nothing over the guard.** At the levels that require a bookend, the guard
  already signs the whole body and the definer header.
- **Memory-only state.** A restart forgets nonces, clearances, approvals, idempotency records
  (including "outcome unknown" ones), and contract pins. After a restart, the gateway trusts
  the first tool listing it sees again.
- **Stdio servers run as the gateway's user.** Each one can read what the gateway reads (on
  macOS, the guard-key file and every secret), read the other servers' environment, write the
  audit log, and signal the gateway, forcing a restart that empties its memory. Run only stdio
  servers you trust as much as the gateway ([Deploying](../guide/deploy.md#who-can-do-what)).
- **Arguments are checked for form only.** The gateway enforces
  [AR-1 to AR-5](addressing.md#arguments), such as strict JSON and unique query keys, but does
  not validate arguments against the tool's `inputSchema` or refuse duplicate keys inside a
  JSON body.
- **Open-world reads are not gated.** A read-only open-world tool (a fetcher, a search API) can
  carry data out in its arguments with neither a clearance nor an approval, at any level, unless
  its tier is raised: `sensitive` brings a clearance from level 3, and `dangerous` also a person
  at level 4 ([RQ-1](methods.md#requirements)).
- **Taint is declared, not observed.** `X-UFO-Taint` marks open-world output as untrusted, and
  acting on it is left to the harness. The reference shim (`gateway/examples/shim.py`) has no
  taint policy: it mints a clearance for every call that needs one unless you give it a `vouch`
  function. The gateway does not track which data steered a call.
- **Destinations are listed.** `GET /` on the domain itself returns every destination with its
  level, its transport, and whether the gateway is connected to it, without the guard. The
  reference proxies answer `421` for the bare domain, so it is reachable only from the host.
  The `404 unknown_service` answer lists the destinations only under loopback names.
- **Audit gaps.** Discovery, nonce issuance, and a few framework-level refusals are not audited,
  a request is served even when its entry cannot be written, and removing the tail of the log
  is not detectable from the file alone ([AU-1, AU-3](audit-deployment.md#audit)). Ship the
  log, or at least its latest hash, off the host.
- **Idempotency is best effort under load.** When a destination's store is full, completed
  records are evicted oldest first, and a retry whose record was evicted runs again
  ([ID-5](levels.md#level-2-bound)).
- **A key is needed even at level 0.** The reference gateway needs a guard key
  (`WEBSPEC_GUARD_KEY` or `WEBSPEC_GUARD_KEY_FILE`) for every unsafe request at every level
  ([GD-5](levels.md#level-1-signed)).
- **The reference deployments leave some rules to the operator.** None of them sets up DP-3:
  the agent's egress is confined by the operator ([Deploying](../guide/deploy.md#egress-dp-3)).
  The macOS deployment ships no proxy, so DP-5 and DP-6 there rest on the operator's own, and
  DP-2 has no macOS equivalent. In the Docker stack, Docker lets Caddy's published port go
  whenever Caddy's container is down, so DP-9 holds there for the gateway's port only, and a
  process that holds the port when Docker starts Caddy again keeps it until it stops.
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
- Provenance: an `X-UFO-Provenance` header that records who a request passed through, such as
  a person and then an agent, with each link signed, bound to the request fingerprint and
  nonce, and enforced by the gateway. Today the gateway ignores the header.
- Refuse duplicate keys in JSON bodies.

**Addressing**

- Per-qualifier backends, with edge support (Caddy host blocks and per-destination wildcard
  certificates).

**Audit and approval**

- Off-host audit shipping (or at least the latest hash), and failing closed at level 3 and
  above when the log cannot be written.
- Audit discovery and nonce issuance.
- An out-of-band approval device, such as a phone push that shows the summary.

**Registry** (`gateway/webspec_registry`)

The registry is an experimental tool catalog outside this spec. It ranks the gateway's tools
against a text query by keyword overlap. It serves on loopback by default, has no
authentication, and is not part of the reference deployments.

- Exposure behind the guard.
- Semantic matching.
- Ranking by recency, by preference, and by whether a tool's destination is connected.

**Deployment**

- A proxy configuration for macOS.
- A proxy listener that only the tunnel can reach, so that Caddy tells tunneled requests from
  local ones by where they arrive, not by headers a local process can send.
- Stdio servers under users of their own, and a watchdog for a gateway that a signal stopped.
- Egress tooling for DP-3.

**Conformance**

- A conformance suite and a vulnerability scanner that test each rule by its ID.

## Open questions

1. **Path word order.** In what order should a path name what it is, what it does, and what it
   is done to ([Path](addressing.md#path))?
2. **Retire the bookend?** It adds nothing over the guard ([Known limits](#known-limits)).
3. **Clearance key.** Until independent signers exist, should level 3 at least use a key
   separate from the guard key?
4. **Public index.** Should the bare domain stop listing destinations?
5. **Open-world reads.** Should a read-only open-world tool need a clearance at level 3 (and a
   person at level 4) by default, rather than only when it is raised to `dangerous`?

## Versions

- **2026-10.** The spec was rewritten to match the gateway: method binding, tool contracts,
  levels 0–4, and reference deployments for Linux, macOS, and Docker. Earlier drafts are
  retired and remain in the git history.
