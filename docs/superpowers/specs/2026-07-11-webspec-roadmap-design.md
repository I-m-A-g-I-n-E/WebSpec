# WebSpec Roadmap & Design — Self-Hostable (B) with Multi-Tenant (C) Scaffolding

**Date:** 2026-07-11
**Status:** Approved design — ready for implementation planning
**Author:** Design session (Preston + Claude Fable 5)

> **Purpose of this document.** WebSpec is currently a live personal deployment (tier A) whose
> published spec describes a much larger product than the code implements. This document defines
> the next milestone — a **self-hostable** WebSpec (tier B) — and *locates* (without building) the
> **multi-tenant** future (tier C). It is written to be executed by someone other than its author:
> every tier-B behavior is defined precisely enough to become a failing test, and every tier-C
> extension point is named and mapped so it can be picked up later. The companion implementation
> plan (produced next, via the planning workflow) turns this into ordered, checkpointed steps.

---

## 0. Tiers (the organizing principle)

| Tier | Meaning | This round |
|------|---------|------------|
| **A** | Personal gateway — one trusted operator, host-coupled (current live state) | starting point |
| **B** | Self-hostable — a stranger can `docker compose up` their own instance with their own secrets | **BUILD (test-first)** |
| **C** | Multi-tenant service — one deployment, many users, OAuth/JWT, per-user keychains | **LOCATE (`TODO(C)` + roadmap)** |

**Rule for every component below:** B behavior ships with failing tests first (the tests *are* the
spec). C behavior ships as `# TODO(C): …` markers in code plus an entry in `docs/ROADMAP-C.md`
that maps the marker to its section here. Nothing about C is implemented; everything about C is findable.

---

## 1. Starting reality (what actually exists today)

Grounding facts, verified 2026-07-11 on the live host:

- **Live path:** `*.i-a-m.live` → cloudflared → Caddy `:7001` → gateway (uvicorn) `:7002` → MCP service.
  Both `caddy-webspec.service` and `webspec-gateway.service` are active system units.
- **Catalog:** 3 MCP servers in `~/.claude.json` — `mail-proton` (stdio, **guard=false**),
  `op-auth` (stdio, guard), `supabase` (http, guard).
- **Implemented:** HTTP→MCP bridge (path = literal MCP tool name, slash→underscore fallback);
  HMAC guard (`session_key` + single-use audience-bound nonce); definer verb families;
  op-auth UFO tiers (open/sensitive/dangerous, run() allowlist, audit log, `/__challenge`).
- **Not implemented (docs describe it anyway):** clean REST `/collection/id` hierarchy, format
  suffixes, DELETE, OAuth/JWT, NLP resolution, embedding registry, three-way join, keychain wiring.
- **Known defects to fix in this round:**
  - `mail-proton` is an **unauthenticated public email relay** (guard=false + public tunnel; only gate
    is a non-secret `X-Gimme-Definer: SEND` header). Verified reachable.
  - Guard/definer HMACs truncate to 32 bits (tamper-evidence strength, not auth strength).
  - CORS is `allow_origins=["*"]` on a gateway that proxies 1Password.
  - Gateway binds `0.0.0.0:7002` (LAN-reachable, bypasses Caddy rate limiting).
  - `docs/` self-contradicts (see §6) and duplicates `wiki/`.

---

## 2. Architecture: the decomposition

Four units. **Track A** is independent and can run end-to-end alone. The **Trunk** is a shared
foundation. **B1** and **B2** both grow on the Trunk and can then proceed in parallel.

```
                      ┌─────────────────────────────────────────┐
   Track A (ship)     │  docker-compose: Caddy + gateway +       │   independent —
   ───────────────    │  registry service; hardening; docs       │   runs without B
                      └─────────────────────────────────────────┘

                      ┌─────────────────────────────────────────┐
   Trunk (shared)     │  Catalog + Normalization                 │   build once
   ───────────────    │  harvest OPTIONS → (service,tool,verb,    │
                      │  noun,tier,schema) records               │
                      └───────────────┬──────────────┬──────────┘
                                      │              │
        ┌─────────────────────────────┘              └──────────────────────────┐
        ▼                                                                        ▼
  ┌──────────────────────────┐                          ┌──────────────────────────────┐
  │ Track B1 — Resolver      │                          │ Track B2 — Keychain graph     │
  │ embed/rank catalog;      │                          │ 1P metadata → merge catalog → │
  │ "what tool does X";      │                          │ services→nouns→verbs poset    │
  │ keychain leg via op-auth │                          │ graph + secrets catalog       │
  └──────────────────────────┘                          └──────────────────────────────┘
```

Both B1 and B2 are exposed by a single new **`registry` service** (its own subdomain, in the
compose stack), so they are testable in isolation and route through the existing gateway model.

---

## 3. Track A — Self-hostable & hardened

### 3.1 Auth model: the password manager IS the session authority

**Decision (approved).** Eliminate the gateway's separate `~/.webspec/session.key`. The guard key
becomes a secret sourced from a password manager (1Password in the reference implementation). This
is on-thesis: WebSpec's trust root *is* the keychain.

**Mechanism (B):**

- The gateway reads its guard key from the environment variable **`WEBSPEC_GUARD_KEY`**.
- That variable is populated **from the vault at process launch** — via `op run -- …` or
  `op read "op://<vault>/<item>/<field>"` in the container entrypoint / systemd `ExecStartPre`.
  The key never lands in a plaintext file the way `session.key` did; its lifetime is the process.
- `config.get_session_key()` is rewritten: decode `WEBSPEC_GUARD_KEY` (hex/base64) to 32 bytes.
  **Fail closed** if absent — do NOT silently generate a random key (that would resurrect the
  second secret). A documented dev-only escape hatch (`WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1`) may mint
  an ephemeral in-memory key for local testing, printed loudly as insecure.
- Authorized **clients** obtain the same key by reading the same vault item (their own biometric /
  service-account unlock gates that read). Requests are signed exactly as today (HMAC + nonce).
- **Keep the nonce store** unchanged — replay protection is orthogonal to key source.
- The gateway process is headless, so it authenticates to 1Password with a **service account**
  (`OP_SERVICE_ACCOUNT_TOKEN`), not biometrics. Biometric unlock is a *client-side* human gesture only.

This leaves **one** root of trust (the vault), down from two (vault + `session.key`).

**`TODO(C)` here:**
- Pluggable secret backend (1Password Connect, other password managers, HashiCorp Vault) behind a
  `SecretBackend` interface. B ships only the `op`-CLI backend.
- Per-user / asymmetric keys: replace the single shared symmetric key with per-user vault items or
  client-holds-private-key / gateway-holds-public-key (Ed25519) so tenants can't sign for each other.
- 1Password-issued bearer validated per-request (a "real" server-side session), instead of a shared
  symmetric secret.

### 3.2 Hardening pass (each item = a test)

1. **Public exposure requires guard.** Invariant: a service with `guard=false` must not be reachable
   on the public domain. Enforce at two layers — the Caddy generator refuses to write a public host
   block for an unguarded service (localhost block only), and the gateway refuses guard-less requests
   whose `Host` is the public domain. This closes the mail-proton relay by construction, not by a
   one-off flag.
2. **Bind localhost.** Gateway default bind becomes `127.0.0.1`; `0.0.0.0` only via explicit
   `WEBSPEC_HOST` override (documented as "you are fronting it yourself").
3. **Lock CORS.** Default `allow_origins` to an empty/explicit allowlist from `WEBSPEC_CORS_ORIGINS`;
   never wildcard by default. Guarded services shouldn't be browser-reachable cross-origin at all.
4. **Guard-by-default.** New services provisioned via `webspec-ctl` default to `guard=true`.
   `--no-guard` requires the service to be localhost-only (ties to invariant #1).
5. **HMAC strength note.** Keep 4-byte truncation for the *definer* (it's a tamper-evidence
   checksum, documented as non-auth), but document explicitly that the *guard* HMAC's security rests
   on the nonce + key secrecy, not the 32-bit tag. `TODO(C)`: widen guard tag to ≥16 bytes when the
   header-length budget allows, or move to Ed25519 signatures.

### 3.3 Config externalization (kills host-path assumptions)

- `WEBSPEC_CONFIG` (default `~/.claude.json`) — path to the MCP-server catalog.
- `WEBSPEC_ENV_FILE` (default `~/.env`) — secret placeholders.
- Guard key via `WEBSPEC_GUARD_KEY` (§3.1). Remove all direct `Path.home()` couplings from the
  request path; they become defaults, not hardcodes.

### 3.4 Docker topology: a compose stack (not one image)

- **`docker/Dockerfile`** — builds gateway + registry (one Python image, two entrypoints).
- **`docker/docker-compose.yml`** — services: `caddy` (fronts `:7001`, generated conf.d),
  `gateway` (`:7002`, localhost within the compose network), `registry` (`:7003`).
- **The `registry` service runs `guard=true`.** It exposes your full tool inventory, service graph,
  and 1Password account catalog — sensitive by definition — so it is subject to the same
  "public exposure requires guard" invariant (§3.2 #1) and must never be reachable unguarded.
- **stdio MCP services stay host/sidecar.** op-auth (`op` CLI + service token) and mail-proton
  (Protonmail Bridge) can't be generically containerized for a stranger. Ship a documented
  **"bring your own MCP service"** pattern: http-transport services join the stack as containers;
  stdio services run on the host and are referenced by the mounted `WEBSPEC_CONFIG`.
- **`webspec-ctl` compose mode.** `ctl.py` currently shells to `systemctl`/`caddy reload`. Add a
  compose-native path (regenerate conf.d + `caddy reload` via the admin API) so provisioning works
  inside the stack. Host/systemd mode remains for tier-A users.

### 3.5 Docs reconciliation (see §6 for the specifics)

Part of Track A because you must not ship an image whose docs describe a different system.

---

## 4. Trunk — Catalog + Normalization (shared by B1 & B2)

New module set in a sibling **`webspec_registry`** package (recommended over folding into the gateway
package, so the registry service stays independently deployable and testable).

### 4.1 `catalog.py`

- `harvest(services) -> list[ToolRecord]` — for each service, fetch its tools (name, description,
  inputSchema). Source of truth is the gateway's existing `OPTIONS /{service}/` response, consumed
  over HTTP so the registry stays decoupled from the pool internals.
- `ToolRecord = {service, tool, description, input_schema, verb, noun, tier}`.

### 4.2 `normalize.py`

- `normalize(tool, description) -> (verb, noun)`:
  - Seed map from the docs' predicate/object tables (`send/post/notify → send`, `note/dm → message`, …).
  - Heuristic fallback: split `snake_case` tool names (`send_email → verb=send, noun=email`),
    lemmatize lightly, canonicalize against the seed vocabulary.
- `tier_of(service, tool) -> "open"|"sensitive"|"dangerous"` — reuse op-auth UFO tiers where the
  service exposes them; default per method (GET→open, mutating→sensitive) otherwise.
- Partial order: `open ⊑ sensitive ⊑ dangerous` (the poset's ordering relation for §5.2).

### 4.3 Tests (B)

- `test_catalog_harvest.py` — harvest yields ToolRecords for the live/mock services; missing service
  degrades gracefully.
- `test_normalize.py` — `send_email → (send,email)`; `read → (read,·)`; unknown tool → heuristic;
  tier mapping matches UFO for op-auth.

---

## 5. Track B1 (resolver) & B2 (keychain graph)

### 5.1 B1 — Semantic resolver

- `resolver.py`: `resolve(query, catalog, user_ctx) -> list[Ranked]`.
- `Matcher` interface: `score(query, record) -> float`.
  - **Default `LexicalMatcher`** — token overlap + verb/noun canonicalization. Zero external deps;
    B works with no API key and no model download.
  - **Optional `EmbeddingMatcher`** — local small model (e.g. a bundled ONNX/sentence-transformer)
    or an API backend, selected by config. Off by default.
- Ranking = `matcher_score × status_weight × recency × preference`, where `status_weight` implements
  the three-way join legs: **connected** (in pool) > **keychain** (op-auth has creds) > **available**.
- **Keychain leg via op-auth**: query op-auth *metadata* to decide whether a service's credential
  exists (see §5.2 rules — metadata only, never secret values).
- Exposed on the `registry` service: `GET registry.<domain>/resolve?q=…` → ranked JSON.

**`TODO(C)`:** multi-vector search (canonical/predicate/object indices), per-user preference &
history boosts, negative-example repulsion, query expansion. (All described in `docs/discovery/*`.)

### 5.2 B2 — Keychain graph & secrets catalog

- `keychain.py`: read 1Password **metadata** via op-auth `list_vaults` / `list_items` / `get_item`
  → `AccountRecord = {vault, item, title, category, urls, fields_present[], updated_at,
  linked_gateway_service?}`.
  - **Hard invariant (test-enforced): never call `op-auth.read()` and never surface secret values.**
    Only titles, categories, URLs, field *names*, timestamps leave 1Password.
- `graph.py`: merge catalog (§4) + accounts → a directed graph.
  - **Nodes:** services, nouns, verbs, accounts (1P items).
  - **Edges:** `service —exposes→ (verb,noun)`; `account —credentials→ service` (matched by URL/name);
    `(verb,noun) —tier→ {open|sensitive|dangerous}`.
  - **Poset:** the combinatorial ordering the user asked for — nouns partially ordered by
    containment (e.g. `message ⊑ channel ⊑ workspace`), verbs grouped by definer family, tiers by §4.2.
  - **Outputs:** JSON (nodes/edges), Graphviz **DOT**, and a **secrets catalog** table
    `{service, vault, item, category, has_credential, last_updated, linked_gateway_service}`.
- Exposed on `registry`: `GET /graph` (JSON/DOT), `GET /catalog` (secrets table).

**`TODO(C)`:** balance/quota/liveness enrichment — per-provider billing calls to answer "does this
API key still have credit / an active account." No generic shortcut; one adapter per provider.

### 5.3 Tests (B)

- `test_resolver_ranks.py` — "fire off a note to Sarah" ranks a send/message tool first;
  connected > keychain > available ordering holds; lexical matcher works with no model present.
- `test_keychain_metadata_only.py` — the keychain reader never invokes `read()`; asserts no field
  value from a 1P item appears in output (property test over a mock vault).
- `test_graph_output.py` — graph contains expected service/noun/verb/account nodes and edges; DOT
  renders; poset ordering respects tier and noun-containment relations.

---

## 6. Docs reconciliation (Track A deliverable)

The published spec must describe the system that ships. Concrete actions:

1. **Resolve the core contradiction.** `docs/url-grammar/*` bans `object.provider` path syntax
   (`message.slack` labeled "WRONG"), but `docs/discovery/*` is built entirely on that banned form
   (`POST /message.slack`). Pick one model and make both sections agree. Since the *code* routes by
   MCP tool name, document that as the normative reality and mark REST-hierarchy/format-suffixes as
   proposed.
2. **Collapse redundancy.** `url-grammar/core-syntax.md`, `complete-grammar-ebnf.md`, and
   `object-type-system.md` re-explain "subdomain IS provider" three times → one normative grammar
   page + one examples page.
3. **Add an Implemented-vs-Proposed status matrix** to `docs/index.md` so no reader mistakes vision
   for reality. Every "Draft" section gets an honest tag: *Implemented (B)* / *Proposed (C)* /
   *Conceptual*.
4. **Reconcile domain & ports:** docs say `gimme.tools` and gateway-on-7001; reality is `i-a-m.live`
   and Caddy-on-7001 / gateway-on-7002. State the real topology; keep `gimme.tools` as the docs/site
   and the C-tier public vision, explicitly labeled.
5. **De-duplicate `docs/` vs `wiki/`.** They are near-identical hand-maintained mirrors. Make `wiki/`
   generated from `docs/` (or drop it). Single source of truth.
6. **`docs/ROADMAP-C.md` (new):** the index of every `TODO(C)` marker → the §here that specifies it.

---

## 7. Test strategy & "done" for B

- Framework: `pytest` (matches existing `gateway/tests/`).
- **Test-first**: each B behavior above gets a failing test before implementation.
- Layers: unit (normalize, matcher, keychain-metadata invariant), component (resolver ranking, graph
  output, guard-key-from-vault, public-exposure-requires-guard), integration
  (`test_compose_smoke.py` — `docker compose up`, then an end-to-end request through Caddy→gateway).
- **Definition of done for B** = all B tests green + `docker compose up` yields a working stack with
  no host-path assumptions + docs status matrix present + `session.key` removed and guard key sourced
  from the vault + no unguarded service reachable on the public domain.

---

## 8. C — located, not built (summary index)

Every item lives as `# TODO(C):` in the nearest relevant module and in `docs/ROADMAP-C.md`:

- Pluggable `SecretBackend`; per-user / asymmetric guard keys; 1P-bearer per-request session (§3.1).
- Wider guard HMAC tag / Ed25519 signatures (§3.2).
- Multi-vector semantic search, per-user preference/history, query expansion, negative examples (§5.1).
- Provider billing/quota/liveness enrichment (§5.2).
- OAuth/JWT identity, multi-tenant config & isolation, format suffixes, REST collection hierarchy,
  DELETE method (the aspirational `docs/` surface not needed for B).

---

## 9. Execution notes (for whoever runs this without the author)

- Tracks are independent: **A**, and **Trunk→(B1 ∥ B2)**. A can be done first, last, or in parallel.
- Each track should become its own implementation plan (spec → plan → build) if handed to separate
  agents. This document is the shared source of truth they all reference.
- Start every track from its failing tests. If a test here is ambiguous, the test file is the tie-breaker;
  make the ambiguity explicit there.
- The one ordering constraint: **Trunk before B1/B2**; **docs reconciliation before publishing an image**.
```
