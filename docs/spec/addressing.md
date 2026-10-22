# Addressing

A WebSpec request names its destination in the host, its tool in the method and the path, and
its arguments in the query or the body:

``` { .text .ws-softwrap }
METHOD https://{destination}.{domain}/{noun}?{arguments}
```

The path is a noun, what the request acts on, and the method is the verb. `POST` covers many
kinds of action, so on `POST` the definer names the verb ([DF-3](methods.md#per-method-rules)).
A tool keeps its whole name as its path when its name is not a verb of the
[route rule](#the-route-rule) followed by `_` and a noun, when that verb's method is not
admissible for the tool's contract, or when its entry collides with another
([PA-4](#the-route-rule), [PA-6](#building-the-map)).

Up to four qualifiers may precede the destination, as in `eu.mail.example.com`
([HG-3](#host)), but the reference gateway refuses every one of them for now ([HG-5](#host)).

## Host

```
host        = 0*4( qualifier "." ) destination "." domain
destination = label      ; selects the service, and no other label does
qualifier   = label      ; optional routing selector: region, version, environment, …
label       = [a-z0-9] ( [a-z0-9-]{0,61} [a-z0-9] )?
```

- **HG-1** The destination, which is the label immediately left of the domain, is the only label
  that selects a service. Contracts, levels, nonce and clearance audiences, idempotency scopes,
  and approval queues are all kept per destination. A destination that is not configured is
  answered with `404 unknown_service`.

- **HG-2** Every label MUST be a lowercase DNS label as defined above. Otherwise the gateway
  answers `404 invalid_host`.

- **HG-3** Each qualifier MUST appear in its destination's allow-list (configuration key
  `labels`). A qualifier MUST appear at most once, qualifiers MUST follow allow-list order, and a
  host MUST carry at most four. Otherwise the gateway answers `404 unknown_qualifier`. The
  default allow-list is empty, so a destination with no `labels` is reachable only as
  `{destination}.{domain}`.

- **HG-4** Qualifiers MAY change *where* a request lands. They MUST NOT change *what* it means
  (that is the method, the path, and on `POST` the definer) or *who* is asking (that is the
  signed credentials). Labels MUST be safe to publish and MUST NOT carry tenant or user
  identity.

- **HG-5** A gateway that cannot route a qualifier to a separate backend MUST refuse an
  allow-listed qualifier with `404 qualifier_not_routable`. It MUST NOT serve that qualifier
  from the destination's default backend. Serving `eu.mail.example.com` from the same backend as
  `mail.example.com` would suggest regional or data-residency routing that does not exist. The
  reference gateway has no per-qualifier backends, so it refuses every qualifier
  ([roadmap](status.md#roadmap)).

- **HG-6** A gateway MUST NOT honor a nonce, clearance, approval, or idempotency key issued for a
  different destination.

- **HG-7** A destination below level 1 MUST NOT be served on a public domain (`403
  unguarded_public`). It is served only under loopback names such as `{destination}.localhost`,
  which keep it on the machine only while [DP-5 and DP-8](audit-deployment.md#deployment) hold.

**Why qualifiers can route but never isolate.** The web already ties a qualifier to its
destination:

- A cookie set with `Domain=mail.example.com` is also sent to `eu.mail.example.com`.
- Every host under one registrable domain is *same-site*.
- A wildcard certificate covers exactly one label, so hosts with qualifiers need per-destination
  wildcards (`*.mail.example.com`), and those are published in Certificate Transparency logs.

A qualifier therefore lives inside its destination's trust zone and is public by construction.
Isolation comes from the destination label, which is its own origin.

**Destination names.** The reference gateway derives each destination from the name of its
MCP server in the configuration. It removes every bracketed part, such as `[id=4SNMTV]`,
lowercases the name, turns each run of underscores or whitespace into one hyphen, and removes
every character that is not an ASCII letter, digit, or hyphen. Then it collapses repeated
hyphens and trims them from both ends, so `MCP_DOCKER` becomes `mcp-docker`. A name that comes
out longer than 63 characters is not a valid label, so every request for it gets
`404 invalid_host` ([HG-2](#host)). Error codes such as `unknown_service`, the `service` field
in responses, and `webspec-ctl` call a destination a *service*.

## Path

- **PA-1** The method, the path, and on `POST` the definer select exactly one tool of the
  destination, through the destination's *route map*. The map gives each tool exactly one
  entry: a method, a path, and on `POST` a definer ([PA-4](#the-route-rule)). The path is a
  single segment. Once its surrounding slashes are trimmed, as the guard signs it
  ([GD-2](levels.md#level-1-signed)), a path that still contains `/` matches nothing, so
  `/send/email` does not reach `send_email`. Item ids and every other argument stay in the query
  or the body ([AR-2 to AR-5](#arguments)), as in `DELETE /note?id=welcome`.

- **PA-2** These paths are reserved:

    | Request | Meaning |
    |---|---|
    | `GET /` | List the destination's tools and their entries ([DS-3](methods.md#discovery)) |
    | `HEAD /`, `OPTIONS /` | Describe the destination: its health, and its route map ([DS-1, DS-2](methods.md#discovery)) |
    | `GET /__nonce` | Issue a nonce, on destinations at level 1 or higher ([GD-4](levels.md#level-1-signed)) |
    | `GET /__challenge` | Retired. Answers `410 Gone` on destinations at level 1 or higher. Level-4 approval challenges come in a `428` answer instead ([AP-1](levels.md#level-4-witnessed)) |

    Where a reserved path applies, it is answered before the route map, so a tool whose entry
    falls on it cannot be reached there.

- **PA-3** `POST`, `PUT`, `PATCH`, or `DELETE` on `/` is refused with `400 missing_tool`.

### The route rule

- **PA-4** The gateway MUST give each tool its entry by this procedure, then move entries as
  [PA-6](#building-the-map) says. The procedure reads only the tool's name and its contract
  ([TC-1 to TC-6](methods.md#tool-contracts)). It uses no synonyms and no registry, and it never
  pluralizes a path.
    1. Split the tool's name at its first `_` into `first` and `rest`. If the name has no `_`,
       `rest` is empty.
    2. Look up `first` in the table below, exactly as written. If it is there and `rest` is not
       empty, the candidate is the table's method, the path `/{rest}` verbatim, and on `POST`
       the table's definer. Otherwise the candidate is `POST /{name}` with the definer `INVOKE`,
       where `{name}` is the full tool name.
    3. The candidate's method is *admissible* if both hold: [MB-1](methods.md#method-binding)
       admits it for the tool's contract, and it is not `PUT` or `DELETE` for a tool that is not
       idempotent. A non-idempotent tool is never routed to `PUT` or `DELETE`, the methods that
       RFC 9110 §9.2.2 defines as idempotent.
    4. If the method is admissible, the candidate is the entry. Otherwise the entry is
       `/{name}`, with the fallback method below. The fallback method is always admissible.

    | `first` | Method | Definer |
    |---|---|---|
    | `get`, `read`, `list`, `search` | `GET` | — |
    | `set`, `replace` | `PUT` | — |
    | `update`, `modify`, `patch`, `append`, `rename` | `PATCH` | — |
    | `delete`, `remove` | `DELETE` | — |
    | `create`, `add` | `POST` | `CREATE` |
    | `send` | `POST` | `SEND` |
    | `upload` | `POST` | `UPLOAD` |
    | `trigger` | `POST` | `TRIGGER` |

    | Contract | Fallback method |
    |---|---|
    | read-only | `GET` |
    | destructive and idempotent | `DELETE` |
    | anything else | `POST`, with the definer `INVOKE` |

Examples:

| Tool | Contract | Entry |
|---|---|---|
| `read_note` | read-only | `GET /note` |
| `delete_note` | destructive, idempotent | `DELETE /note` |
| `send_email` | not read-only, not destructive | `POST /email` with `WebSpec-Definer: SEND` |
| `run_query` | not read-only, not destructive | `POST /run_query` with `WebSpec-Definer: INVOKE` |
| `get_weather` | unannotated, so destructive and not idempotent ([TC-2](methods.md#tool-contracts)) | `POST /get_weather` with `INVOKE`, because `GET` is not admissible |
| `delete_draft` | unannotated, so destructive and not idempotent | `POST /delete_draft` with `INVOKE`, because the tool is not idempotent |
| `get_and_delete_x` | destructive, not idempotent | `POST /get_and_delete_x` with `INVOKE`, so `GET /get_and_delete_x` answers `405` with `Allow: HEAD, OPTIONS, POST` |
| `read` | read-only, no `_` in the name | `GET /read` |

A server that re-lists `get_and_delete_x` as read-only does not move it: its contract stays
destructive, because the gateway keeps the strictest contract it has seen
([TC-6](methods.md#tool-contracts)).

- **PA-5** The route map only narrows [MB-1](methods.md#method-binding). It binds each tool to
  one admissible method and never adds a method. MB-1 and MB-2 still apply to every request,
  against the contract of the tool that the entry resolves to, so an entry whose method that
  contract does not admit answers `405` ([MB-2](methods.md#method-binding)).

### Building the map

- **PA-6** An entry's *key* is its method, its path, and on `POST` its definer. On other methods
  the definer is not part of the key. Keys are compared in the form a request reaches them, for
  collisions, pins, and requests alike: the path with its surrounding slashes trimmed
  ([PA-1](#path)). An entry whose path, in that form, is empty or contains `/` matches no
  request, and the gateway logs its tool as unreachable. The gateway builds a destination's map
  the first time it lists the destination's tools after it starts. If several entries share a
  key, each of them moves to `/{name}` and keeps its method and definer, and an entry already at
  `/{name}` stays where it is. This repeats until no entry can move. A key that entries still
  share then answers `503 route_collision` (PA-7). No tool wins, and no tool takes another tool's
  key. The gateway logs one line that names
  the tools it moved, then binds and pins every key (PA-8).

- **PA-7** The gateway rebuilds the map, and checks it again, in one atomic step every time it
  lists the destination's tools again and every time it reloads the configuration. A rebuild
  gives each tool its entry as PA-4 does, except that a tool the first build moved stays at
  `/{name}`. If two or more entries share a key in a rebuild, that key answers
  `503 route_collision` until a rebuild finds it unshared. The gateway never picks one of them.
  A key that is new, has never been pinned, and is not shared is bound and pinned (PA-8).

- **PA-8** **Binding pins.** For each key it binds, the gateway pins the tool that the key
  resolves to. If a rebuild resolves a pinned key to a different tool, the key answers
  `503 binding_changed`, naming both tools, until the operator confirms the change in the
  destination's configuration key `bindings`. Each `bindings` entry maps a key, written
  `METHOD /path` and on `POST` followed by a space and the definer, to a tool name:

    ```json
    "bindings": {"POST /email CREATE": "add_email"}
    ```

    An entry that names the tool the key now resolves to re-pins the key to that tool, and the
    key is served again. An entry only confirms: it never sends a key to a tool that the route
    rule did not give it, and it never opens a key that two or more tools share
    (`503 route_collision`, PA-7). If a pinned key resolves to no tool after a rebuild, because
    its tool was removed or now has another key, a request on it gets the `404` or `405` of the
    new map (PA-10) and never reaches another tool. The gateway audits both the change and the
    confirmation ([AU-5](audit-deployment.md#audit)). Binding pins MUST survive the destination
    being removed from the configuration and added back, as contract pins do
    ([TC-6](methods.md#tool-contracts)).

A binding pin defends against the rug pull of [TC-6](methods.md#tool-contracts) done by renaming:
a server that replaces `create_email` with an `add_email` that has a looser contract lands on the
same key, `POST /email` with `CREATE`, but cannot take it over unnoticed. A tool renamed onto a
different key, such as `send_email` renamed to `create_email`, gets a key of its own, and
`POST /email` with `SEND` answers `404`. `create_email` itself is a new tool, with the contract
it is listed with ([known limits](status.md#known-limits)).

### Allow and refusals

- **PA-9** `Allow` describes a path. It lists the methods mapped on the path whose tool MB-1
  currently admits, plus `HEAD` and `OPTIONS`, in this order: `HEAD`, `OPTIONS`, `GET`, `POST`,
  `PUT`, `PATCH`, `DELETE`. A key that answers `503` (PA-7, PA-8) counts as mapped, and its
  method is listed. On the demo server of [A request at every level](../guide/walkthrough.md),
  `/note` maps `GET` to `read_note` and `DELETE` to `delete_note`, so its `Allow` is
  `HEAD, OPTIONS, GET, DELETE`.

- **PA-10** The gateway resolves a `GET`, `POST`, `PUT`, `PATCH`, or `DELETE` request on a tool
  path in this order, and answers with the first refusal that applies:
    1. The path maps nothing: `404 tool_not_found`.
    2. The path does not map the method: `405 method_not_allowed`, with the path's `Allow`.
    3. On `POST`, the definer ([DF-1, DF-2](methods.md#per-method-rules)). A missing definer is
       `400 missing_definer`, and a value that is not one verb of `POST`'s family is
       `400 unknown_definer` or `400 definer_family_mismatch`. A `POST` verb that maps no tool
       on the path is `404 tool_not_found`, and its `detail` lists the definers that the path
       maps.
    4. The key is closed: `503 route_collision` (PA-7) or `503 binding_changed` (PA-8).
    5. The tool's contract does not admit the method: `405 method_not_allowed`
       ([MB-2](methods.md#method-binding)).

    `HEAD` and `OPTIONS` stop after step 1, and are answered as
    [DS-1 and DS-2](methods.md#discovery) say, even when a key on the path is closed.

## Arguments

- **AR-1** A query key MUST NOT repeat (`400 duplicate_query_key`). If keys could repeat, the
  tool would see only one of the values, and no signature could bind the value the tool actually
  used. Encode lists as one JSON value.

- **AR-2** A `GET` that invokes a tool carries its arguments only in the query string, and its
  body MUST be empty (`400 body_not_allowed`).

- **AR-3** If a query parameter's type in the tool's `inputSchema` cannot be a string (such as
  `integer`, `number`, `boolean`, `array`, or `object`), its value MUST parse as strict JSON, as
  in `?limit=5&ids=["a","b"]`. A value that doesn't parse is refused with
  `400 invalid_arguments`. Every other value, including one for a parameter with no declared
  type, is passed as a string. The gateway does not check a parsed value against its type, so
  `?limit=true` reaches the tool as `true`.

- **AR-4** A non-empty body MUST be a single strict-JSON object: no `NaN`, no `Infinity`, and no
  numbers that overflow. Anything else is refused with `400 invalid_arguments`. The reference
  gateway does not yet refuse duplicate keys inside the object; the last one wins
  ([known limits](status.md#known-limits)).

- **AR-5** An argument MUST NOT appear in both the query and the body (`400
  invalid_arguments`).

The [per-method rules](methods.md#per-method-rules) say where each method carries its
arguments.
