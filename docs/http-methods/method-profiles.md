# Method Profiles

> **Status: Implemented (B)** — `gateway/webspec/{methods,handlers,idempotency,approval,audit,hostgrammar,hardening}.py`,
> tests in `gateway/tests/test_method_profiles_http.py`, `test_methods.py`, `test_units_misc.py`.
> Items marked **Proposed (C)** are specified but not built; see [ROADMAP-C](../ROADMAP-C.md).
>
> The key words MUST, MUST NOT, SHOULD, and MAY are used as in RFC 2119.

A WebSpec URL describes exactly **what it is, what it does, and what's done to**. That makes
it convenient for an AI to write and for a human to read. Per-method profiles make it
*trustworthy*: the gateway knows what each tool really is, so a request cannot describe itself
as something weaker than it is.

## Why per-method rules need method binding

Every HTTP method gets its own rule set (below). But rules attached to the method alone are
worthless if the **caller** picks the method: an attacker — or a coerced agent — simply calls a
destructive tool with `GET`, the method with the weakest rules. Before this profile, the gateway
did exactly that: `GET /send_email?...` sent mail with no definer at all.

**Rule 0 (binding).** The gateway, not the caller, decides which methods may invoke a tool, as a
function of the tool's *contract*. A request whose method the contract does not admit MUST be
refused with `405 Method Not Allowed` and an `Allow` header listing the admissible methods.

The method is the *genus* of the action (safe / idempotent / destructive — the class the web
already understands); the path names the *species* (`send_email`). The definer verb sits between
them. All three must agree.

---

## 1. Host grammar

```
host        = *( qualifier "." ) destination "." domain
destination = label     ; the service — the only label that routes, the only isolation boundary
qualifier   = label     ; optional routing selector (region, version, environment, …)
label       = [a-z0-9] [ *61( [a-z0-9-] ) [a-z0-9] ]
```

- The **destination** (the label immediately left of the domain) MUST be the only label used for
  routing, audience binding, guard nonces, contracts, and levels.
- **Qualifiers** MUST each appear in the destination's own allow-list (config key `"labels"`), at
  most once, in the allow-list's order. The default allow-list is empty, so an unconfigured
  service accepts exactly `{destination}.{domain}` — "two words, company and product".
- Qualifiers MUST only change *where* a request lands, never *what* it means (that is the path)
  or *who* is asking (that is the signed credential).
- Labels MUST be lowercase; anything else is `404 invalid_host`. Unknown or misordered
  qualifiers are `404 unknown_qualifier`.

**Why labels can route but never isolate.** The web gives label hierarchy a meaning whether we
want it or not: a cookie scoped to `slack.example` is sent to `eu.slack.example`, and every label
under one registrable domain is *same-site*. Wildcard certificates cover exactly one label, so
deeper names need `*.slack.example` certificates, which are published in Certificate
Transparency logs. Therefore qualifiers live *inside* their destination's trust zone and MUST be
safe to be public. Tenant or user identity in a label would violate both properties.

Edge support for qualifier hosts (Caddy blocks, per-destination wildcard certificates) is
**Proposed (C)**; the gateway enforces the grammar today on any host it receives.

---

## 2. Tool contracts

A tool's contract is five facts:

| Field | Meaning | MCP source |
|---|---|---|
| `read_only` | does not modify its environment | `readOnlyHint` |
| `destructive` | may destroy/overwrite (vs. only add) | `destructiveHint` |
| `idempotent` | repeating it has no further effect | `idempotentHint` |
| `open_world` | touches external, untrusted entities | `openWorldHint` |
| `tier` | `open` ⊑ `sensitive` ⊑ `dangerous` | `_meta["webspec/tier"]` (raise-only) |

**Sources, in precedence order:**

1. **Operator override** — `mcpServers.<name>.tools.<tool>` in the gateway config. Authoritative;
   MAY loosen. An override with unknown keys or wrongly typed values MUST fail closed to the
   *strictest* contract (destructive, non-idempotent, open-world, `dangerous`).
2. **Server annotations** — MCP `ToolAnnotations`. Trusted only because the operator chose to
   configure the server; remote servers SHOULD be given explicit overrides.
3. **MCP defaults** — an unannotated tool is *not* read-only, *is* destructive, *is not*
   idempotent, *is* open-world. Unannotated means strict.

A server-declared tier MAY only raise the tier derived from annotations (`open` for read-only,
`sensitive` otherwise); it can never lower it.

**Pinning (rug-pull defense).** Servers can re-list their tools at any time. The gateway MUST keep,
per (service, tool), the join (least upper bound) of every contract it has observed. A re-listing
can therefore tighten a contract immediately but can never loosen it; only an operator override
loosens. Pins are per-process today; persisting an operator-approved snapshot is
**Proposed (C)**.

---

## 3. Method binding

| Contract | Admissible invoking methods |
|---|---|
| read-only | `GET` |
| additive (not destructive) | `POST`, `PATCH` (+ `PUT` if idempotent) |
| destructive | `DELETE` (+ `PUT` if idempotent, else `POST`) |

`HEAD` and `OPTIONS` are available on every tool path and MUST NOT invoke the tool.
`OPTIONS /{tool}` returns the contract, the admissible methods, and the requirements for each at
the service's level, so an AI can learn exactly what to send before sending it.

---

## 4. Levels — the security dial

Each service has a level from 0 to 4 (config key `"level"`). Raising the level only ever **adds**
requirements; the requirement function is monotone in the level (test-enforced).

| Level | Name | Adds |
|---|---|---|
| 0 | local | Grammar rules only: binding, definers, empty `GET` body. Loopback only. |
| 1 | signed | Guard: HMAC over method, host, path, nonce, body, **and canonical query**; single-use, audience-bound nonce. |
| 2 | bound | Tier-2 bookend on every unsafe method; `Idempotency-Key` on `POST`/`PATCH`. |
| 3 | cleared | UFO clearance token, bound to (tool, arguments), for every non-read-only tool and every `sensitive`/`dangerous` tool. |
| 4 | witnessed | Human approval (an Ed25519 signature over the exact request) for every destructive tool and every `dangerous` tool. |

- Absent `level`: 1 if `"guard": true`, else 0 (backward compatible). `guard: true` forces level ≥ 1;
  level ≥ 1 implies the guard. An invalid level MUST fail closed to 4.
- A service on the public domain MUST be at level ≥ 1 (existing invariant: unguarded services are
  localhost-only).

---

## 5. Per-method profiles

| Method | Safe | Idempotent | Invokes | Body | Definer family | Level ≥ 2 adds |
|---|---|---|---|---|---|---|
| `HEAD` | yes | yes | never | — | — | — |
| `OPTIONS` | yes | yes | never | — | — | — |
| `GET` | yes | yes | read-only tools | MUST be empty | — | — |
| `POST` | no | no | additive / non-idempotent destructive | arguments (JSON) | `CREATE SEND INVOKE TRIGGER UPLOAD` | bookend, `Idempotency-Key` |
| `PUT` | no | yes | idempotent tools | arguments | `REPLACE OVERWRITE SET` | bookend |
| `PATCH` | no | no | additive tools | arguments | `MODIFY APPEND AMEND RENAME` | bookend, `Idempotency-Key` |
| `DELETE` | no | yes | destructive tools | arguments (query preferred) | `REMOVE REVOKE ARCHIVE CANCEL PURGE` | bookend |

- **GET** MUST carry arguments in the query string only; a non-empty body is `400 body_not_allowed`.
  `GET` never needs a definer — which is exactly why it must never reach a mutating tool.
- **POST / PUT / PATCH / DELETE** MUST carry `X-Gimme-Definer` from their own family
  (`400 missing_definer` / `definer_family_mismatch`). At level ≥ 2 the definer MUST carry the
  Tier-2 bookend (`403 bookend_required`). `DELETE` joins the definer families with this profile,
  closing [Definer Verbs](definer-verbs.md) open question 1.

## 6. Contract and tier rules

The requirements of a request are the union **method rules ∪ contract rules ∪ tier rules**, each
gated by level:

- **Level ≥ 3:** a valid `X-UFO-Clearance` token, bound to the exact (tool, arguments) and expiring
  after 30 s, for any tool that is not read-only or whose tier is above `open`
  (`403 clearance_missing|invalid|expired|malformed`). This is where the UFO tiers documented for
  op-auth are now actually enforced; before this profile nothing checked them.
- **Level ≥ 4:** human approval (§8) for any destructive tool or `dangerous` tool.

Clearance proves that the *caller's harness* vouched for the call; it is only as strong as the
policy that mints it (§12). Level 4 is the only level that holds against a fully compromised
harness, because the gateway holds only *public* keys for it.

---

## 7. Idempotency (POST, PATCH)

Agents retry. For non-idempotent methods the gateway implements the IETF HTTPAPI
`Idempotency-Key` semantics, scoped per destination:

| Same key, then… | Response |
|---|---|
| same request fingerprint, completed | the stored response, `Idempotent-Replayed: true`; the tool is **not** called again |
| different fingerprint | `422 idempotency_key_reused` |
| first attempt still running | `409 idempotency_key_in_flight` |
| first attempt timed out / lost its connection | `409 idempotency_outcome_unknown` — the tool may have run; the client MUST verify, then use a new key |

The fingerprint is SHA-256 over method, path, canonical query, and SHA-256 of the body. The key
is optional below level 2 (honored if sent) and required at level ≥ 2. A replay is checked
*before* one-shot credentials (clearance, approval) so a legitimate retry never needs a second
human signature; the key is claimed atomically only after those checks pass.

## 8. Human approval (level 4)

1. A request that needs approval and lacks `X-WebSpec-Approval` gets `428 Precondition Required`
   with `{challenge, fingerprint, summary, sign_message, namespace, expires_in}`. The `summary` is
   the canonical description of *exactly* this request (method, destination, host, path, tool,
   arguments, body hash); `fingerprint = SHA-256(canonical JSON of summary)`.
2. A human runs `webspec-ctl approve` on that body. It MUST re-derive the fingerprint from the
   summary it displays and refuse on mismatch (*what you see is what you sign*); it MUST display
   the summary with all control and non-ASCII characters escaped; it MUST read confirmation from
   the controlling terminal, never stdin. It signs `sign_message` with
   `ssh-keygen -Y sign -n webspec-approval` (or a compatible signer such as 1Password's
   `op-ssh-sign`) and prints the header.
3. The client retries the identical request with `X-WebSpec-Approval: <challenge>:<signature>`.
   The gateway verifies with OpenSSH (`ssh-keygen -Y verify`) against `WEBSPEC_APPROVERS_FILE`
   (an allowed-signers file; restrict entries with `namespaces="webspec-approval"`).

Challenges are single-use and expire after 300 s; a challenge cannot approve a different request
(`approval_mismatch`), a second time (`approval_reused`), or with an unlisted key
(`approval_invalid`). If no approvers are configured, level-4 requests fail closed
(`503 approval_unavailable`). The retired `/__challenge` endpoint returns `410 Gone`: it minted a
challenge that nothing ever verified.

The signing key SHOULD live in an agent that requires a biometric gesture per signature
(1Password SSH agent, a Secure Enclave–backed key). Residual risk: a human who reflexively
approves a biometric prompt they did not initiate defeats this level. Approval delivered on a
separate device (phone push showing the summary) is **Proposed (C)**.

## 9. Signing the request

At level ≥ 1 the guard HMAC covers the query string. Clients MUST sign the **canonical query**:
decode all pairs, keep blank values, sort by (key, value), and re-encode with RFC 3986
percent-encoding (unreserved characters `A–Z a–z 0–9 - . _ ~` literal; spaces as `%20`). The
canonical query is appended to the signed message as `:?<canonical-query>` **only when non-empty**,
so requests without a query sign exactly as before. Previously the query was unsigned, so the
arguments of every guarded `GET` could be altered in flight.

## 10. Response obligations

Every tool invocation response MUST carry `Cache-Control: no-store`, `X-WebSpec-Level`, and
`X-WebSpec-Tier`. If the tool is open-world, it MUST carry `X-UFO-Taint: open-world`, and the
harness MUST treat the body as untrusted (UFO-tagged) data that cannot authorize later calls.

## 11. Audit

Every decision — invoked, denied (including `405` probes), challenged, replayed — MUST be
appended to a hash-chained JSON-lines log (`WEBSPEC_AUDIT_LOG`, default
`~/.webspec/gateway-audit.jsonl`). Each line records the URL-addressed action in the clear
(method, host, path, tool, level, tier, risk, outcome) and the query and body only as hashes;
each line carries the SHA-256 of the previous line, so an edit or deletion breaks the chain
(`audit.verify_chain`). Off-host shipping and fail-closed auditing at level ≥ 3 are
**Proposed (C)**.

## 12. Deployment requirements

- The gateway MUST run as a **dedicated OS user**, not the agent's user. Same-user processes can
  read another process's environment (`/proc/<pid>/environ` on Linux, `KERN_PROCARGS2` on macOS),
  and the guard key lives in the gateway's environment.
- On Linux the gateway marks itself **non-dumpable** at startup (`prctl(PR_SET_DUMPABLE, 0)`),
  closing `/proc/<pid>/environ`, `/proc/<pid>/mem`, and ptrace to same-user processes, including
  the stdio MCP servers it spawns (those already receive only a minimal environment from the MCP
  SDK, never the guard key).
- The gateway SHOULD be the agent's **only network egress**, enforced at the network layer
  (firewall / network namespace / egress proxy allow-list). A chokepoint only holds if nothing
  routes around it.
- The audit log SHOULD be writable only by the gateway's user (or shipped off-host), so the agent
  cannot rewrite history.

## 13. The harness shim — easy for the AI

The model writes only what it is good at: **method, host, path, arguments, and one definer
word**. A harness shim (never the model) adds everything cryptographic — guard HMAC and nonce,
bookend, `Idempotency-Key`, clearance — so no key ever enters the model's context, and text the
model reads cannot forge them. The shim learns what to add from `OPTIONS /{tool}`.

---

## 14. Threat model: what 2026 actually looked like

The incidents below are taken from the vendors' and investigators' own reports (dates as
published). They are why these rules exist, and they also mark what the rules do **not** stop.

| Observed behavior | Incident | Control here | Effect |
|---|---|---|---|
| Destructive / supply-chain step (modify repo, open PR, trigger CI beacon) | OpenAI agents vs. Hugging Face, Jul 2026 (OpenAI technical report, Aug 26 2026) | Destructive contract → `DELETE`/`PUT` + level-4 human approval | **Stops** at level 4 |
| Calling a mutating tool through a weaker verb | gateway behavior before this profile | Method binding (`405`) | **Stops** |
| Read credentials from `/proc/self/environ` / process memory | HF dataset-server file read (Jul 2026); early Claude Mythos snapshot (system card, Apr 2026) | Dedicated user + non-dumpable; level 4 holds only public keys | **Stops** same-user reads of the gateway |
| Replay / forged calls with stolen request material | general | Single-use nonce; HMAC over method, host, path, **query**, body | **Stops** |
| Concealment: rewriting git history, spoofed tool-call logs, rewriting activity when challenged | Mythos system card; METR/Redwood (Aug 2026); UK AISI incident report (Aug 2026) | Hash-chained gateway audit log as ground truth | **Detects** (prevents only if the log is out of the agent's reach) |
| Task decomposition into innocuous-looking steps | GTG-1002 espionage via Claude Code (Anthropic, Nov 2025) | Audit sequence + human gate on the *terminal* destructive step | **Slows / detects**; per-step rules alone do not catch it |
| Indirect prompt injection through fetched content | Claude Code Auto Mode bypass (Embrace The Red, Aug 2026); ChatGPT Atlas | `X-UFO-Taint` on open-world output; clearance at level 3 | **Slows** — only as good as the shim's taint policy |
| Weaponized *arguments* to an allowed tool (malicious HDF5, template injection) | HF intrusion, Jul 2026 | none yet | **Not touched** — needs argument validation (Proposed C) |
| C2 over an allowed channel (polling a public dataset; directory-name message boards) | HF intrusion; Artifactory "message board", May–Jul 2026 | Audit + rate limits (edge) | **Detects at best** |
| Reaching hosts *around* the gateway; harvested third-party credentials used directly | HF intrusion (Modal relay); UNCTAD relay workarounds, 2026 | Gateway as sole egress (§12) | **Stops only if enforced at the network layer** |

The through-line of those reports is that capable, persistent agents treat security boundaries as
obstacles to route around. So every control here is **mandatory and fail-closed**, never
advisory: unannotated means strict, invalid config means strictest, missing approvers mean
refusal, and contracts can only be loosened by the operator.

## 15. Relationship to Apple's platform security model

| Apple | WebSpec |
|---|---|
| Entitlements — signed, declarative capabilities bound to code identity | Tool contracts; operator overrides are the grants; pinning is the signature stability |
| App Sandbox (Seatbelt) — deny by default | Unannotated = strict; method binding; per-destination isolation |
| TCC consent + Touch ID / Secure Enclave user presence | Level-4 approval signed by a biometric-gated key |
| XPC privilege separation | Gateway and each MCP server as separate processes and OS users |
| Hardened Runtime (no task-port / debugger attach) | Non-dumpable gateway process |
| Private Cloud Compute's verifiable transparency log | Hash-chained audit log |

Entitlement keys are reverse-DNS (`com.apple.security.network.client`); WebSpec hosts are the same
naming tree read forward.

## 16. Open questions and Proposed (C)

1. **Path word order.** The path should say what it is, what it does, and what's done to; the
   canonical order of those words is not yet fixed. Today the path is the MCP tool name.
2. **Proof of read.** `If-Match` with a gateway-issued ETag (or signed read receipt) on
   `PUT`/`PATCH`/`DELETE`, so an agent can only overwrite what it has seen.
3. **Argument validation** against each tool's `inputSchema` (type-coercing query strings for
   `GET`).
4. **Wider tags.** Guard, bookend, and clearance tags are 32-bit; widen to ≥ 128-bit or move to
   Ed25519 request signatures.
5. **Provenance v2.** Today's provenance links are 16-bit, unbound to the request, and not
   checked; bind them to the request fingerprint and nonce before enforcing them.
6. Persistent contract pins; off-host, fail-closed audit; out-of-band approval device; edge
   (Caddy/TLS) support for qualifier labels.

## Conformance summary

| Requirement | L0 | L1 | L2 | L3 | L4 |
|---|---|---|---|---|---|
| Method binding (`405` + `Allow`) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Empty `GET` body; definer on unsafe methods | ✓ | ✓ | ✓ | ✓ | ✓ |
| Guard HMAC (incl. query) + single-use nonce | | ✓ | ✓ | ✓ | ✓ |
| Tier-2 bookend on unsafe methods | | | ✓ | ✓ | ✓ |
| `Idempotency-Key` on `POST`/`PATCH` | | | ✓ | ✓ | ✓ |
| UFO clearance (non-read-only or tier > open) | | | | ✓ | ✓ |
| Human approval (destructive or dangerous) | | | | | ✓ |
| Audit, `no-store`, taint marking | ✓ | ✓ | ✓ | ✓ | ✓ |
