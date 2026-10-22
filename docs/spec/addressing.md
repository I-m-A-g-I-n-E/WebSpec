# Addressing

A WebSpec request names its destination in the host, its tool in the path, and its arguments
in the query or the body:

``` { .text .ws-softwrap }
https://{destination}.{domain}/{tool}?{arguments}
```

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
  (that is the path) or *who* is asking (that is the signed credentials). Labels MUST be safe to
  publish and MUST NOT carry tenant or user identity.

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

- **PA-1** The path names exactly one tool of the destination. In the reference gateway the path
  is the MCP tool name, and a path that matches no tool is retried with `/` folded to `_`, so
  `/send/email` reaches `send_email`. If no tool matches, the gateway answers
  `404 tool_not_found`.

- **PA-2** These paths are reserved:

    | Request | Meaning |
    |---|---|
    | `GET /` | List the destination's tools ([DS-3](methods.md#discovery)) |
    | `HEAD /`, `OPTIONS /` | Describe the destination ([DS-1, DS-2](methods.md#discovery)) |
    | `GET /__nonce` | Issue a nonce, on destinations at level 1 or higher ([GD-4](levels.md#level-1-signed)) |
    | `GET /__challenge` | Retired. Answers `410 Gone` on destinations at level 1 or higher. Level-4 approval challenges come in a `428` answer instead ([AP-1](levels.md#level-4-witnessed)) |

- **PA-3** `POST`, `PUT`, `PATCH`, or `DELETE` on `/` is refused with `400 missing_tool`.

The path is meant to read as *what it is, what it does, and what it is done to*, in a word
order that is still an [open question](status.md#open-questions).

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
