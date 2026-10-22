# Methods and tool contracts

The method says what kind of act a request is. The tool's **contract** says what kind of act
the tool performs. The gateway refuses any request in which the two disagree, so a caller cannot
reach a destructive tool with `GET`, the method with the fewest requirements
([threat model](../guide/threat-model.md#the-design-in-five-decisions)).

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
  be able to lower it.

- **TC-4** A read-only tool is treated as non-destructive and idempotent. Its `openWorldHint`
  still applies.

- **TC-5** An operator override is authoritative and MAY loosen a contract. Overrides live in
  the destination's configuration as `tools.{tool}`, with any of the five fields.

    - An empty or false-like override (`{}`, `false`, `0`, `[]`) is ignored, and so is an empty
      or false-like `tools` value.
    - An override with an unknown key or a wrongly typed value MUST fail closed to the
      *strictest* contract: destructive, non-idempotent, open-world, and `dangerous`. So must
      any other `tools` value that is not an object, for every tool of the destination.
    - An override does not re-derive the tier, so an override that changes `read_only` should
      also set `tier`.
    - An override that sets `read_only` to false on a read-only tool MUST start from the strict
      defaults (tier `sensitive`), not from the read-only contract, even if the server had
      raised the tier. Starting from the read-only contract would make the tool weaker than an
      unannotated one.

- **TC-6** **Pinning.** For each destination and tool, the gateway MUST keep the *join* of every
  contract it has observed from the server, and use that join instead of the latest listing.
  The join takes the stricter value of each field: read-only only if both contracts are,
  destructive if either is, idempotent only if both are, open-world if either is, and the higher
  tier. A server can tighten a contract but can never loosen it. A tightening takes effect the
  next time the gateway lists the server's tools, which in the reference gateway is within 5
  minutes or on reconnect. Only an operator override loosens. Pins MUST survive the service
  being removed from the configuration and added back. Otherwise editing the configuration
  would reset them.

Pinning defends against a "rug pull": a server that re-lists a destructive tool as read-only to
unlock `GET`. A binding pin ([PA-8](addressing.md#building-the-map)) stops a renamed tool from
taking over a key that another tool held. A tool listed under a new name, on a key that no tool
held, is a new tool: its contract is pinned from that listing, and overrides written for the
old name do not apply to it. The reference gateway keeps pins in memory, so a restart resets
them, and a stdio server, which runs as the gateway's user, can force a restart
([known limits](status.md#known-limits)).

## Method binding

| Contract | Methods that may invoke the tool |
|---|---|
| read-only | `GET` |
| neither read-only nor destructive | `POST`, `PATCH`, plus `PUT` if idempotent |
| destructive | `DELETE`, plus `PUT` if idempotent or `POST` if not |

- **MB-1** The gateway, not the caller, decides which methods may invoke a tool. It decides from
  the tool's contract, according to the table above.

- **MB-2** If the contract does not admit the request's method, the gateway MUST refuse it with
  `405 method_not_allowed` and the path's `Allow` header
  ([PA-9](addressing.md#allow-and-refusals)).

- **MB-3** `HEAD` and `OPTIONS` are available on every tool path, and they MUST NOT invoke the
  tool.

## Per-method rules and definers { #per-method-rules }

| Method | Safe | Arguments | Definer | Definer family |
|---|---|---|---|---|
| `HEAD` | yes | none | ignored | none |
| `OPTIONS` | yes | none | ignored | none |
| `GET` | yes | query only, empty body | ignored | none |
| `POST` | no | JSON object body and/or query | required | `CREATE` `SEND` `INVOKE` `TRIGGER` `UPLOAD` |
| `PUT` | no | JSON object body and/or query | optional | `REPLACE` `OVERWRITE` `SET` |
| `PATCH` | no | JSON object body and/or query | optional | `MODIFY` `APPEND` `AMEND` `RENAME` |
| `DELETE` | no | query and/or JSON object body | optional | `REMOVE` `REVOKE` `ARCHIVE` `CANCEL` `PURGE` |

- **DF-1** Every `POST` MUST carry `WebSpec-Definer` (`400 missing_definer`). A `PUT`, `PATCH`,
  or `DELETE` MAY carry it. A definer sent on one of these four methods MUST be a verb of that
  method's family (`400 definer_family_mismatch`). On `GET`, `HEAD`, and `OPTIONS` a definer is
  not checked, echoed, or logged, though the guard still signs it
  ([GD-2](levels.md#level-1-signed)).

- **DF-2** A request MUST carry at most one `WebSpec-Definer` field line. Two or more are
  `400 unknown_definer`, whatever the method. On `POST`, `PUT`, `PATCH`, and `DELETE` the value
  is exactly one verb of the families above, matched without regard to case. Anything else
  there is `400 unknown_definer` too: an empty value, a value with a `:` (the bookend form of
  earlier versions), a value with a comma (as when a server or proxy combined two field lines,
  RFC 9110 §5.3), or a word that is in no family.

- **DF-3** On `POST` the definer names the action; on other methods it is optional and only
  confirms. On `POST` it is part of the route key, so it selects the tool among the `POST`
  entries on the path ([PA-6, PA-10](addressing.md#path)). On `PUT`, `PATCH`, and `DELETE` it
  never selects a tool and never changes the method's rules. Either way, a definer that is sent
  is signed by the guard ([GD-2](levels.md#level-1-signed)) and logged
  ([AU-2](audit-deployment.md#audit)).

**The verb, signed with its contents.** The clearance ([CL-1](levels.md#level-3-cleared)) and
the approval ([AP-1](levels.md#level-4-witnessed)) bind the method and the tool that the request
resolves to. They do not take the definer as an input. On `POST` the tool is the one the definer
selected, so they bind the action that the definer named, and the verb is signed with its
contents without any new input. On `PUT`, `PATCH`, and `DELETE` the definer is checked against
the method's family only, never against the tool: `ARCHIVE` is accepted on a tool that deletes
outright.

## Requirements

- **RQ-1** A request must carry every item below whose condition holds:

    | Requirement | Applies when |
    |---|---|
    | Guard (HMAC and nonce) | the level is 1 or higher, on every request except `GET /__nonce`, which carries the tag without a nonce ([GD-4](levels.md#level-1-signed)), and the retired `GET /__challenge` |
    | Definer | the method is `POST` |
    | Empty body | the method is `GET` |
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
  `Cache-Control: no-store`, `WebSpec-Level`, and `WebSpec-Tier`.

- **RS-2** If the tool is open-world, the response MUST carry `WebSpec-Taint: open-world`. The
  harness MUST treat that body as untrusted data, which cannot authorize later calls.

- **RS-3** A successful call answers `200 {"result": …}`. A tool that reports an error (MCP
  `isError`) answers `422 {"result": …, "error": true}`. Each text block that parses as strict
  JSON is returned as JSON, and several blocks become an array. MCP `structuredContent` is not
  passed through. If a definer was checked ([DF-1, DF-2](#per-method-rules)), the body also
  gives its verb, in upper case, as `canonical`.

- **RS-4** The gateway refuses with a JSON object `{"error": code, "detail": text, …}` and the
  status that the rule names. A few answers come from the web framework before the gateway's
  handlers run, and they have plain-text bodies: a host that matches no route (`404`), a method
  not in the [per-method table](#per-method-rules) (`405`), a rejected CORS preflight (`400`),
  and an unhandled error (`500`). A request that needs the guard key while no usable key is set
  also gets a plain-text `500` ([GD-5](levels.md#level-1-signed)). Every refusal, the plain-text
  ones included, MUST carry `Cache-Control: no-store`, so that no cache keeps a `404` or `405`
  after the route map changes.

Failures after commit ([CO-1](levels.md#commit)), when any clearance and approval have already
been spent:

| Status | Code | Meaning |
|---|---|---|
| `502` | `tool_rejected` | The server answered with a JSON-RPC error, so the tool did not run. The idempotency key is released |
| `503` | `service_unavailable` | The connection to the server failed or closed during the call. The tool may have run, and the idempotency key is marked *outcome unknown* |
| `504` | `tool_timeout` | No answer within 30 s. The tool may have run, and the idempotency key is marked *outcome unknown* |
| `500` | `result_unserializable` | The tool ran, but its result could not be returned |

After a timeout or a broken connection, the reference gateway drops its connection to the
server and reconnects on the next request.

## Discovery

- **DS-1** `OPTIONS /{noun}` answers with the path's `Allow`
  ([PA-9](addressing.md#allow-and-refusals)) and a body with one entry for each method mapped on
  the path, and on `POST` one entry for each definer. If the path maps nothing, it answers
  `404 tool_not_found`. Each entry names its method, and on `POST` its definer, and gives the tool
  it resolves to (`tool`), the tool's `inputSchema`, its `contract`, and the `requirements` for
  that method at the destination's level. `requirements` has one boolean for each row of
  [RQ-1](#requirements): `guard`, `definer`, `empty_body`, `idempotency_key`, `clearance`, and
  `approval`. That is enough for a client to build a correct request before it sends one, except
  for a level-4 approval, which needs the challenge from the `428` answer to a first attempt
  ([AP-1](levels.md#level-4-witnessed)). An entry whose key is closed names no tool and gives,
  as `error`, the code it answers: `route_collision` or `binding_changed`. `OPTIONS /` returns
  the entries of every path, with their paths: the whole route map.

- **DS-2** `HEAD /{noun}` answers `200` with `Allow` and `Link: </>; rel="service-desc"`
  (RFC 8631), or `404` if the path maps nothing. It never invokes a tool, so it reports the
  route, not whether an item exists. That departs from RFC 9110 §9.3.2, under which `HEAD` would
  answer as `GET` does. `HEAD /` reports health in `WebSpec-Status` (`connected` or
  `degraded`), the number of tools in `WebSpec-Tool-Count`, and the destination in
  `WebSpec-Service`.

- **DS-3** `GET /` lists the tools with their descriptions and their entries: the method, the
  path, and on `POST` the definer. Every discovery response MUST carry
  `Cache-Control: no-store`.

On a destination at level 1 or higher, discovery requests carry the guard like any other
request. On the demo server of [A request at every level](../guide/walkthrough.md),
`OPTIONS /note` has two entries, `GET` to `read_note` and `DELETE` to `delete_note`, and
`OPTIONS /email` has one, `POST` with `SEND` to `send_email`.

`contract.source` says where the contract came from: `annotations`, `default` (the server
gave none), `override`, `pinned` (a join of different sources), or `strictest` (an invalid
override).
