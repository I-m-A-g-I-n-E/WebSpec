# Addressing

A WebSpec request names its destination in the host, its tool in the path, and its arguments
in the query or the body:

```
https://{qualifier}*.{destination}.{domain}/{tool}?{arguments}
```

The host keeps two words for the company and the product: the domain and the destination.
Everything after them describes what the request is, what it does, and what it is done to.

## Host

```
host        = *( qualifier "." ) destination "." domain
destination = label      ; the service, and the only label that routes
qualifier   = label      ; optional routing selector: region, version, environment, …
label       = [a-z0-9] ( [a-z0-9-]{0,61} [a-z0-9] )?
```

- **HG-1** The destination, which is the label immediately left of the domain, is the only label
  that selects a service. Contracts, levels, nonce and clearance audiences, idempotency scopes,
  and approval queues are all kept per destination.
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
  from the destination's default backend. The reference gateway refuses every qualifier today:
  serving `eu.mail.example.com` from the same backend as `mail.example.com` would suggest a
  regional or data-residency routing that does not exist.
- **HG-6** A gateway MUST NOT honor a nonce, clearance, approval, or idempotency key issued for a
  different destination.
- **HG-7** A destination below level 1 MUST NOT be served on a public domain (`403
  unguarded_public`). It is reachable only under loopback names such as
  `{destination}.localhost`.

**Why labels can route but never isolate.** The web gives label hierarchy a meaning whether we
want it or not:

- A cookie scoped to `mail.example.com` is also sent to `eu.mail.example.com`.
- Every label under one registrable domain is *same-site*.
- A wildcard certificate covers exactly one label, so deeper names need per-destination
  wildcards (`*.mail.example.com`), and those are published in Certificate Transparency logs.

A qualifier therefore lives inside its destination's trust zone and is public by construction.
Isolation comes from the destination label, which is its own origin.

**Destination names.** The reference gateway derives each destination from the name of its
MCP server in the configuration. It drops bracketed suffixes, lowercases the name, turns
underscores and spaces into hyphens, and removes any other character (`MCP_DOCKER` becomes
`mcp-docker`).

## Path

- **PA-1** The path names exactly one tool of the destination. If no tool matches, the gateway
  answers `404 tool_not_found`. In the reference gateway the path is the MCP tool name. A path
  that matches no tool is retried with `/` folded to `_`, so `/send/email` reaches
  `send_email`.
- **PA-2** These paths are reserved:

    | Request | Meaning |
    |---|---|
    | `GET /` | List the destination's tools ([DS-3](methods.md#discovery)) |
    | `HEAD /`, `OPTIONS /` | Describe the destination ([DS-1, DS-2](methods.md#discovery)) |
    | `GET /__nonce` | Issue a nonce, on destinations at level 1 or higher ([GD-4](levels.md#level-1-signed)) |
    | `GET /__challenge` | Retired. Answers `410 Gone` on destinations at level 1 or higher |

- **PA-3** An invoking method other than `GET` on `/` is refused with `400 missing_tool`.

The path is meant to read as *what it is, what it does, and what it is done to*. The canonical
word order for that is an [open question](status.md#open-questions). Today the path is simply
the tool's name.

## Arguments

- **AR-1** A query key MUST NOT repeat (`400 duplicate_query_key`). If keys could repeat, the
  tool would see only one of the values, and no signature could bind the value the tool actually
  used. Encode lists as one JSON value.
- **AR-2** A `GET` request carries its arguments only in the query string, and its body MUST be
  empty (`400 body_not_allowed`).
- **AR-3** If a query parameter's schema type cannot be a string (integer, number, boolean,
  array, or object), its value MUST be strict JSON, as in `?limit=5&ids=["a","b"]`. Every other
  value is passed as a string. A value that doesn't parse is refused with `400
  invalid_arguments`.
- **AR-4** A non-empty body MUST be a single strict-JSON object: no `NaN`, no `Infinity`, and no
  numbers that overflow. Anything else is refused with `400 invalid_arguments` and never
  silently ignored.
- **AR-5** An argument MUST NOT appear in both the query and the body (`400
  invalid_arguments`).

Unsafe methods may carry arguments in the query, the body, or both. `DELETE` usually carries
them in the query.
