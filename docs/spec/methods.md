# Methods and tool contracts

The method says what kind of act a request is. The tool's **contract** says what kind of act
the tool performs. The gateway refuses any request in which the two disagree.

Without this rule, per-method rules would mean nothing. If the caller could pick the method, an
attacker or a coerced agent would call a destructive tool with `GET`, the method with the
weakest rules. So the method states the class of the action (safe, idempotent, or destructive,
which are classes the web already understands), the path names the specific act
(`send_email`), and the definer verb says it aloud. All three must agree.

## Tool contracts

A contract is five facts about a tool:

| Field | Meaning | MCP source |
|---|---|---|
| `read_only` | Does not modify its environment | `readOnlyHint` |
| `destructive` | May destroy or overwrite, not only add | `destructiveHint` |
| `idempotent` | Repeating the call has no further effect | `idempotentHint` |
| `open_world` | Interacts with external, untrusted entities | `openWorldHint` |
| `tier` | `open` < `sensitive` < `dangerous` | `_meta["webspec/tier"]` (can only raise) |

- **TC-1** The contract comes from three sources, in this order of precedence: the operator's
  override, then the server's MCP `ToolAnnotations`, then the MCP defaults.

- **TC-2** Unannotated means strict. Any hint that is missing takes its MCP default: not
  read-only, destructive, not idempotent, and open-world.

- **TC-3** The tier is derived from the server's annotations: `open` for a read-only tool and
  `sensitive` otherwise. A server MAY raise the tier with `_meta["webspec/tier"]`. It MUST NOT
  be able to lower it. An operator override does not re-derive the tier, so an override that
  changes `read_only` should also set `tier`.

- **TC-4** A read-only tool is treated as non-destructive and idempotent. Its `openWorldHint`
  still applies.

- **TC-5** An operator override is authoritative and MAY loosen a contract. Overrides live in
  the destination's configuration as `tools.{tool}`, with any of the five fields. An override
  with an unknown key or a wrongly typed value MUST fail closed to the *strictest* contract:
  destructive, non-idempotent, open-world, and `dangerous`. So must a `tools` value that is not
  an object, for every tool of the destination. An empty or false-like override (`{}`, `false`,
  `0`, `[]`) is ignored. An override that sets `read_only` to false on a read-only tool MUST
  start from the strict defaults (tier `sensitive`), not from the read-only contract, even if
  the server had raised the tier. Starting from the read-only contract would make the tool
  weaker than an unannotated one.

- **TC-6** **Pinning.** For each destination and tool, the gateway MUST keep the *join* of every
  contract it has observed from the server, and use that join instead of the latest listing. A
  server can tighten a contract but can never loosen it. A tightening takes effect the next time
  the gateway lists the server's tools, which in the reference gateway is within 5 minutes or on
  reconnect. Only an operator override loosens. Pins MUST survive the service being removed from
  the configuration and added back. Otherwise editing the configuration would reset them.

The join is the least upper bound in strictness. A tool is read-only only if both contracts
say so. It is destructive if either does (unless read-only), idempotent only if both are, and
open-world if either is. Its tier is the higher of the two. Pinning defends against a "rug
pull": a server that re-lists a destructive tool as read-only to unlock `GET`.

## Method binding

| Contract | Methods that may invoke the tool |
|---|---|
| read-only | `GET` |
| not destructive | `POST`, `PATCH`, plus `PUT` if idempotent |
| destructive | `DELETE`, plus `PUT` if idempotent, otherwise `POST` |

- **MB-1** The gateway, not the caller, decides which methods may invoke a tool. It decides from
  the tool's contract, according to the table above.

- **MB-2** If the contract does not admit the request's method, the gateway MUST refuse it with
  `405 method_not_allowed` and an `Allow` header. The header lists the admissible methods plus
  `HEAD` and `OPTIONS`.

- **MB-3** `HEAD` and `OPTIONS` are available on every tool path, and they MUST NOT invoke the
  tool.

## Per-method rules

| Method | Safe | Invokes | Arguments | Definer family |
|---|---|---|---|---|
| `HEAD` | yes | never | none | none |
| `OPTIONS` | yes | never | none | none |
| `GET` | yes | read-only tools | query only, empty body | none |
| `POST` | no | tools that are neither read-only nor destructive, and destructive tools that are not idempotent | JSON object body and/or query | `CREATE` `SEND` `INVOKE` `TRIGGER` `UPLOAD` |
| `PUT` | no | idempotent tools that are not read-only | JSON object body and/or query | `REPLACE` `OVERWRITE` `SET` |
| `PATCH` | no | tools that are neither read-only nor destructive | JSON object body and/or query | `MODIFY` `APPEND` `AMEND` `RENAME` |
| `DELETE` | no | destructive tools | query and/or JSON object body | `REMOVE` `REVOKE` `ARCHIVE` `CANCEL` `PURGE` |

- **DF-1** Every `POST`, `PUT`, `PATCH`, and `DELETE` MUST carry `X-Gimme-Definer: VERB`, where
  the verb belongs to that method's family. Verbs are case-insensitive. A missing header is
  `400 missing_definer`, an unknown verb is `400 unknown_definer`, and a verb from another family
  is `400 definer_family_mismatch`.

- **DF-2** A definer MAY carry a bookend, written `X-Gimme-Definer: VERB:bookend`. From level 2
  on, the bookend is required (`403 bookend_required`). If a bookend is present, it MUST verify at
  every level (`403 bookend_mismatch`). The bookend is the hex encoding of the first 4 bytes of
  `HMAC-SHA256(key, METHOD ":" VERB ":" head ":" tail)`, where `head` and `tail` are the first
  and last 16 bytes of the body (the whole body if it is 16 bytes or shorter).

- **DF-3** The definer does not select the tool, and it does not change the method's rules. It
  states the intent aloud so that the intent is signed ([GD-2](levels.md#level-1-signed)),
  logged ([AU-2](audit-deployment.md#audit)), and checked against the method.

## Requirements

- **RQ-1** What a request must carry is the union of the method's rules, the contract's rules,
  and the tier's rules, each switched on by the destination's level:

    | Requirement | Applies when |
    |---|---|
    | Guard (HMAC and nonce) | the level is 1 or higher, on every request except `GET /__nonce` (tag only) and the retired `GET /__challenge` |
    | Definer | the method is `POST`, `PUT`, `PATCH`, or `DELETE` |
    | Empty body | the method is `GET` |
    | Bookend | the method is unsafe and the level is 2 or higher |
    | `Idempotency-Key` | the method is unsafe, the tool is not idempotent, and the level is 2 or higher |
    | Clearance | the level is 3 or higher, and the tool is not read-only or its tier is above `open` |
    | Human approval | the level is 4, and the tool is destructive, or `dangerous`, or open-world and not read-only |

- **RQ-2** Raising the level MUST only add requirements. The requirement function is monotone
  in the level.

The approval row covers exfiltration through tools that change something in the open world,
such as sending a message: reading a secret is allowed, so a person witnesses the step that
would send it somewhere. A *read-only* open-world tool, such as a URL fetcher or a search API,
can also carry data out in its arguments, and it needs neither a clearance nor an approval.
Raise such tools to `dangerous` (with `_meta["webspec/tier"]` or an override) so that they need a
clearance at level 3 and a person at level 4. Whether open-world reads should need a clearance
by default is an [open question](status.md#open-questions).

## Responses

- **RS-1** Every response that carries a tool result (`200`, `422`, or a replay) MUST carry
  `Cache-Control: no-store`, `X-WebSpec-Level`, and `X-WebSpec-Tier`.

- **RS-2** If the tool is open-world, the response MUST carry `X-UFO-Taint: open-world`. The
  harness MUST treat that body as untrusted data, which cannot authorize later calls.

- **RS-3** A successful call answers `200 {"result": …}`. A tool that reports an error (MCP
  `isError`) answers `422 {"result": …, "error": true}`. Each text block that parses as strict
  JSON is returned as JSON, and several blocks become an array. MCP `structuredContent` is not
  passed through. If a definer was validated, the response echoes it in
  `X-Gimme-Definer-Canonical` and `X-Gimme-Definer-Tier` (and in `canonical` and `definer_tier`
  in the body).

- **RS-4** The gateway refuses with a JSON object `{"error": code, "detail": text, …}` and the
  status that the rule names. A few answers come from the web framework before the gateway's
  handlers run, and they have plain-text bodies: a host that matches no route (`404`), a method
  outside the seven (`405`), a rejected CORS preflight (`400`), and an unhandled error (`500`).

Failures after the request is accepted:

| Status | Code | Meaning |
|---|---|---|
| `502` | `tool_rejected` | The server answered with a JSON-RPC error, so the tool did not run. The idempotency key is released |
| `503` | `service_unavailable` | The server could not be reached, or the connection closed during the call. In the second case the tool may have run, and the idempotency key is marked *outcome unknown* |
| `504` | `tool_timeout` | No answer within 30 s. The tool may have run, and the idempotency key is marked *outcome unknown* |
| `500` | `result_unserializable` | The tool ran, but its result could not be returned |

In every one of these cases the clearance and the approval, if any, are already spent
([CO-1](levels.md#commit)). After a timeout or a broken connection, the reference gateway drops
its connection to the server and reconnects on the next request.

## Discovery

- **DS-1** `OPTIONS /{tool}` returns the tool's input schema, contract, admissible methods,
  and level, together with the requirements for each admissible method, plus an `Allow` header.
  That is enough for a client to build a correct request before it sends one. `OPTIONS /`
  returns the same information for every tool.

- **DS-2** `HEAD /{tool}` answers `200` with `Allow`, `X-WebSpec-Tool`, `X-WebSpec-Tier`,
  `X-WebSpec-Level`, and `X-WebSpec-Description` (the first 200 characters of the description,
  as ASCII), or `404` if there is no such tool. `HEAD /` reports health in
  `X-WebSpec-Status` (`connected` or `degraded`) and `X-WebSpec-Tool-Count`.

- **DS-3** `GET /` lists the tools with their descriptions and admissible methods.

On a destination at level 1 or higher, discovery requests carry the guard like any other
request. Here is `OPTIONS /send_note` on the [guide's](../guide/walkthrough.md) demo server at
level 4 (with `Allow: HEAD, OPTIONS, POST, PATCH`):

```json
{
  "name": "send_note",
  "description": "Send a note to someone (simulated: queued in an in-memory outbox).",
  "inputSchema": {"type": "object", "required": ["id", "to"],
                  "properties": {"id": {"type": "string"}, "to": {"type": "string"}}},
  "methods": ["PATCH", "POST"],
  "contract": {"read_only": false, "destructive": false, "idempotent": false,
               "open_world": true, "tier": "sensitive", "source": "annotations"},
  "level": 4,
  "level_name": "witnessed",
  "requirements": {
    "PATCH": {"guard": true, "definer": true, "bookend": true, "idempotency_key": true,
              "empty_body": false, "clearance": true, "approval": true},
    "POST":  {"guard": true, "definer": true, "bookend": true, "idempotency_key": true,
              "empty_body": false, "clearance": true, "approval": true}
  },
  "scopes_required": {"PATCH": "PATCH:notes.localhost/send_note",
                      "POST": "POST:notes.localhost/send_note"}
}
```

`contract.source` says where the contract came from: `annotations`, `default` (the server
gave none), `override`, `pinned` (a join of different sources), or `strictest` (an invalid
override). `scopes_required` names the `METHOD:host/path` scope that each method would need
under scoped tokens. Scoped tokens are [proposed](status.md#roadmap). Today every scope is
granted.
