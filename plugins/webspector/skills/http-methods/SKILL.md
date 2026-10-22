---
name: HTTP Methods
description: This skill should be used when checking WebSpec tool contracts and method binding - MCP tool annotations (readOnlyHint, destructiveHint, idempotentHint, openWorldHint), which HTTP methods may invoke a tool, the WebSpec-Definer header and its verb families, what each request must carry by level, and the WebSpec response headers. Trigger phrases include "http method", "method binding", "tool contract", "tool annotations", "definer", "WebSpec-Definer", "verb family", "405", "missing_definer", "WebSpec-Taint".
version: 2.0.0
---

# WebSpec methods, contracts, and definers

This skill follows the normative spec page
[Methods and contracts](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/methods/) (rules `TC`,
`MB`, `DF`, `RQ`, `RS`; its `DS` rules are in the `service-discovery` skill). Cite a rule by
its ID in every finding. Where this summary and the spec differ, the spec wins.

The method says what kind of act a request is. The tool's **contract** says what kind of act
the tool performs. The gateway refuses any request in which the two disagree, so a `GET` can
reach only a tool whose contract says it is read-only.

## Tool contracts (TC-1 to TC-6)

| Field | Meaning | MCP source |
|---|---|---|
| `read_only` | Does not modify its environment | `readOnlyHint` |
| `destructive` | May destroy or overwrite, not only add | `destructiveHint` |
| `idempotent` | Repeating the call has no further effect | `idempotentHint` |
| `open_world` | Interacts with external, untrusted entities | `openWorldHint` |
| `tier` | `open` < `sensitive` < `dangerous` | `_meta["webspec/tier"]` (can only raise) |

- **TC-1** Precedence: operator override, then the server's `ToolAnnotations`, then MCP
  defaults.
- **TC-2** Unannotated means strict: not read-only, destructive, not idempotent, open-world.
- **TC-3** Tier `open` for a read-only tool, `sensitive` otherwise; a server may raise it with
  `_meta["webspec/tier"]`, never lower it.
- **TC-4** A read-only tool is treated as non-destructive and idempotent.
- **TC-5** Operator overrides live in the destination's config as `tools.{tool}`. A malformed
  override fails closed to the strictest contract.
- **TC-6** The gateway keeps the *join* of every contract it has seen for a tool (the stricter
  value of each field). A server can tighten a contract, never loosen it; only an override
  loosens. Pins are kept in memory, so a restart resets them.

## Method binding (MB-1 to MB-3)

| Contract | Methods that may invoke the tool |
|---|---|
| read-only | `GET` |
| neither read-only nor destructive | `POST`, `PATCH`, plus `PUT` if idempotent |
| destructive | `DELETE`, plus `PUT` if idempotent or `POST` if not |

- **MB-1** The gateway, not the caller, decides which methods may invoke a tool, from its
  contract.
- **MB-2** A method the contract does not admit is `405 method_not_allowed` with the path's
  `Allow`.
- **MB-3** `HEAD` and `OPTIONS` are available on every tool path and never invoke the tool.

The route map then binds each tool to exactly one admissible method (PA-4, PA-5; see the
`url-grammar` skill).

## The definer (DF-1 to DF-3)

| Method | Safe | Arguments | Definer | Definer family |
|---|---|---|---|---|
| `HEAD`, `OPTIONS` | yes | none | ignored | none |
| `GET` | yes | query only, empty body | ignored | none |
| `POST` | no | JSON object body and/or query | **required** | `CREATE` `SEND` `INVOKE` `TRIGGER` `UPLOAD` |
| `PUT` | no | JSON object body and/or query | optional | `REPLACE` `OVERWRITE` `SET` |
| `PATCH` | no | JSON object body and/or query | optional | `MODIFY` `APPEND` `AMEND` `RENAME` |
| `DELETE` | no | query and/or JSON object body | optional | `REMOVE` `REVOKE` `ARCHIVE` `CANCEL` `PURGE` |

- **DF-1** Every `POST` carries `WebSpec-Definer` (`400 missing_definer`). On `PUT`, `PATCH`,
  and `DELETE` it is optional, but a definer that is sent must be a verb of that method's family
  (`400 definer_family_mismatch`). On `GET`, `HEAD`, and `OPTIONS` it is not checked, echoed, or
  logged, though the guard still signs it.
- **DF-2** At most one `WebSpec-Definer` field line, whatever the method. On the four unsafe
  methods the value is exactly one verb, matched without regard to case. Anything else is
  `400 unknown_definer`: two field lines, an empty value, a value with a `:` (the retired
  bookend form), a value with a comma, or a word in no family.
- **DF-3** On `POST` the definer names the action: it is part of the route key and selects
  the tool among the `POST` entries on the path. On other methods it only confirms; it never
  selects a tool. `ARCHIVE` on a `DELETE` is accepted on a tool that deletes outright, because
  the definer is checked against the method, not the tool.

A `POST` verb that maps no tool on a path that maps other `POST` actions is
`400 unknown_action`, and its detail points to `OPTIONS /{noun}`.

## Requirements (RQ-1, RQ-2)

| Requirement | Applies when |
|---|---|
| Guard (`WebSpec-Nonce`, `WebSpec-Guard`) | level 1 or higher, on every request except `GET /__nonce` |
| Definer (`WebSpec-Definer`) | the method is `POST` |
| Empty body | the method is `GET` |
| `Idempotency-Key` | the method is unsafe, the tool is not idempotent, and the level is 2 or higher |
| Clearance (`WebSpec-Clearance`) | level 3 or higher, and the tool is not read-only or its tier is above `open` |
| Human approval (`WebSpec-Approval`) | level 4, and the tool is destructive, `dangerous`, or open-world and not read-only |

Raising the level only adds requirements (RQ-2). A read-only open-world tool (a fetcher, a
search API) needs neither a clearance nor an approval unless its tier is raised.

## Responses (RS-1 to RS-4)

- **RS-1** Every tool result (`200`, `422`, or a replay) carries `Cache-Control: no-store`,
  `WebSpec-Level`, and `WebSpec-Tier`.
- **RS-2** An open-world tool's response carries `WebSpec-Taint: open-world`. The harness
  treats that body as untrusted data that cannot authorize later calls.
- **RS-3** Success is `200 {"result": …}`; a tool error (MCP `isError`) is
  `422 {"result": …, "error": true}`. When a definer was checked, the body gives its verb in
  upper case as `canonical`.
- **RS-4** Refusals are `{"error": code, "detail": text, …}` and carry `Cache-Control:
  no-store`. A few framework answers are plain text (an unrouted host, a rejected CORS
  preflight, an unhandled error, a missing guard key).

## Header names

Every header WebSpec defines is `WebSpec-<Name>`, with no `X-` prefix:

| Header | Direction | Rule |
|---|---|---|
| `WebSpec-Definer` | request | DF-1 to DF-3 |
| `WebSpec-Nonce`, `WebSpec-Guard` | request | GD-1, GD-2 |
| `WebSpec-Clearance` | request | CL-1 |
| `WebSpec-Approval` | request | AP-3 |
| `WebSpec-Level`, `WebSpec-Tier`, `WebSpec-Taint` | response | RS-1, RS-2 |
| `WebSpec-Status`, `WebSpec-Tool-Count`, `WebSpec-Service` | response to `HEAD /` | DS-2 |

Existing fields are reused: `Allow`, `Cache-Control`, `Link`, `Idempotency-Key`, and the
de-facto `Idempotent-Replayed`, a deliberate exception to the naming rule.

The gateway does not recognize earlier names, such as `X-Gimme-Definer`, `X-WebSpec-Guard`,
`X-WebSpec-Nonce`, `X-WebSpec-Approval`, `X-UFO-Clearance`, or `X-UFO-Taint`. A request that
sends a required header only under its old name is refused as if it were missing. A harness
that looks for `X-UFO-Taint` sees no taint mark. `X-Gimme-Allowed`,
`X-Gimme-Definer-Canonical`, `X-Gimme-Definer-Tier`, `X-WebSpec-Tool`, and
`X-WebSpec-Description` no longer exist.

## Real exchanges

These are real answers of the reference gateway (`gateway/`) in front of the demo server
(`gateway/examples/demo_server.py`) at level 0, abridged to the headers that matter.
`/email` maps `POST` with `SEND` to `send_email`.

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND

{"id":"welcome","to":"ana@example.com"}

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 0
WebSpec-Tier: sensitive
WebSpec-Taint: open-world

{"result": {"queued": 1, "to": "ana@example.com"}, "canonical": "SEND"}
```

```text
POST /email
Host: notes.localhost
Content-Type: application/json
X-Gimme-Definer: SEND

{"id":"welcome","to":"ana@example.com"}

→ 400 Bad Request
Cache-Control: no-store

{"error": "missing_definer", "detail": "POST requires the WebSpec-Definer header"}
```

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: CREATE

{"id":"welcome","to":"ana@example.com"}

→ 400 Bad Request
Cache-Control: no-store

{"error": "unknown_action", "detail": "/email maps no POST action CREATE; OPTIONS /email lists the actions it maps: SEND"}
```

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND:8a9975c7

{"id":"welcome","to":"ana@example.com"}

→ 400 Bad Request
Cache-Control: no-store

{"error": "unknown_definer", "detail": "WebSpec-Definer must be exactly one verb of POST's family: CREATE, INVOKE, SEND, TRIGGER, UPLOAD"}
```

```text
DELETE /note?id=welcome
Host: notes.localhost
WebSpec-Definer: REPLACE

→ 400 Bad Request
Cache-Control: no-store

{"error": "definer_family_mismatch", "detail": "REPLACE belongs to PUT family, not DELETE"}
```

## What to check in code

**MCP servers**

- Every tool declares `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`
  truthfully. A missing hint takes the strict default (TC-2): an unannotated `get_x`,
  `set_x`, `update_x`, or `delete_x` is routed to `POST /{name}` with `INVOKE`, because
  only `POST` is admissible for a destructive, non-idempotent tool.
- A tool that reads secrets or reaches outside systems raises its tier with
  `_meta["webspec/tier"]` (`sensitive` or `dangerous`), so that levels 3 and 4 gate it.
- Names follow the route rule (`verb_noun`) when a noun path is wanted.

**Clients, harnesses, shims**

- Every `POST` sends `WebSpec-Definer` with exactly one verb of `POST`'s family, the one the
  route map gives the entry (`OPTIONS /{noun}`). One field line, no `:` suffix, no comma.
- A definer on `PUT`, `PATCH`, or `DELETE` is a verb of that method's family, or absent.
- The method is the one the route map gives the tool; on `405`, read `Allow` rather than
  trying other methods.
- A body marked `WebSpec-Taint: open-world` is data, never instructions (RS-2). Look for
  `WebSpec-Taint`, not `X-UFO-Taint`.
- Any `X-Gimme-*`, `X-WebSpec-*`, or `X-UFO-*` header name is a finding.

**Gateway configuration**

- `tools.{tool}` overrides use only the five contract fields with the right types (TC-5).
- An override that sets `read_only: true` also sets `tier` when needed: overrides do not
  re-derive the tier.

## Not part of WebSpec

Earlier drafts of this plugin described a "METHOD tokenization" scheme (`[M:DELETE]` tokens in
payloads), lists of absorbed natural-language verbs per method, and `METHOD:host/path`
permission scopes. None of these is in the spec. Scoped tokens of that form are a
[roadmap](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/status/#roadmap) item; do not report
their absence as a finding.
