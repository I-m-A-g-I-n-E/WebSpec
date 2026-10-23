---
name: URL Grammar
description: This skill should be used when checking how WebSpec requests are addressed - the host (destination, qualifiers, domain), the route rule that gives each MCP tool a method and a single-segment noun path, the route map (collisions, binding pins, Allow), and where arguments go (query or JSON body). Trigger phrases include "webspec url", "webspec path", "route rule", "route map", "tool path", "destination host", "qualifier", "webspec arguments", "405 Allow".
version: 2.0.0
---

# WebSpec addressing: host, route map, arguments

This skill follows the normative spec page
[Addressing](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/addressing/) (rules `HG`, `PA`,
`AR`). Cite a rule by its ID in every finding. Where this summary and the spec differ, the spec
wins.

A request names its destination in the host, its tool in the method and the path, and its
arguments in the query or the body:

```text
METHOD https://{destination}.{domain}/{noun}?{arguments}
```

The path is a noun, what the request acts on, and the method is the verb. On `POST`, which
covers many kinds of action, the `WebSpec-Definer` header names the action (see the
`http-methods` skill). WebSpec has no fixed domain: it is the gateway's `WEBSPEC_DOMAIN`, or
`localhost` for loopback names such as `notes.localhost`.

## Host (HG-1 to HG-7)

```text
host        = 0*4( qualifier "." ) destination "." domain
destination = label      ; selects the service, and no other label does
qualifier   = label      ; optional routing selector: region, version, environment, ...
label       = [a-z0-9] ( [a-z0-9-]{0,61} [a-z0-9] )?
```

| Rule | Check | Refusal |
|---|---|---|
| HG-1 | The label left of the domain is the destination, the only label that selects a service | `404 unknown_service` |
| HG-2 | Every label is a lowercase DNS label | `404 invalid_host` |
| HG-3 | Qualifiers are in the destination's `labels` allow-list, once each, in its order, at most four | `404 unknown_qualifier` |
| HG-4 | Qualifiers may change where a request lands, never what it means or who asks; no tenant or user identity in labels | |
| HG-5 | A gateway without per-qualifier backends refuses allow-listed qualifiers (the reference gateway refuses all) | `404 qualifier_not_routable` |
| HG-6 | Nonces, clearances, approvals, and idempotency keys are honored only for the destination they were issued for | |
| HG-7 | A destination below level 1 is not served on a public domain, only under loopback names | `403 unguarded_public` |

## Path (PA-1 to PA-3)

- **PA-1** The method, the path, and on `POST` the definer select one tool through the
  destination's route map. The path is a single segment. Once its surrounding slashes are
  trimmed, a path that still contains `/` matches nothing: `/send/email` does not reach
  `send_email`. Item ids and every other argument stay in the query or the body, as in
  `DELETE /note?id=welcome`.
- **PA-2** Reserved paths, answered before the route map: `GET /` (tools and their entries),
  `HEAD /` and `OPTIONS /` (health and route map), `GET /__nonce` (nonces, level 1 and up), and
  the retired `GET /__challenge` (`410 Gone` at level 1 and up).
- **PA-3** `POST`, `PUT`, `PATCH`, or `DELETE` on `/` is `400 missing_tool`.

## The route rule (PA-4, PA-5)

The gateway gives each tool one entry from its name and its contract alone. No synonyms, no
registry, no pluralizing.

1. Split the tool name at its first `_` into `first` and `rest` (`rest` is empty without a `_`).
2. If `first` is in the table and `rest` is not empty, the candidate is the table's method,
   the path `/{rest}` verbatim, and on `POST` the table's definer. Otherwise the candidate is
   `POST /{name}` with the definer `INVOKE`, where `{name}` is the full tool name.
3. The candidate's method is admissible if MB-1 admits it for the tool's contract and it is
   not `PUT` or `DELETE` for a tool that is not idempotent.
4. If it is admissible, the candidate is the entry. Otherwise the entry is `/{name}` with the
   fallback method.

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
| destructive and idempotent | `PUT` |
| anything else | `POST`, with the definer `INVOKE` |

| Tool | Contract | Entry |
|---|---|---|
| `read_note` | read-only | `GET /note` |
| `delete_note` | destructive, idempotent | `DELETE /note` |
| `send_email` | not read-only, not destructive | `POST /email` with `WebSpec-Definer: SEND` |
| `run_query` | not read-only, not destructive | `POST /run_query` with `INVOKE` |
| `get_weather` | unannotated, so destructive and not idempotent | `POST /get_weather` with `INVOKE` |
| `write_file` | destructive, idempotent | `PUT /write_file` |
| `delete_draft` | unannotated | `POST /delete_draft` with `INVOKE` |
| `read` | read-only, no `_` | `GET /read` |

- **PA-5** The map only narrows MB-1. It binds each tool to one admissible method and never
  adds one; MB-1 and MB-2 still apply to every request.

## Building the map (PA-6 to PA-8)

- **PA-6** An entry's key is its method, its path (slashes trimmed), and on `POST` its
  definer. At the first build after start, entries that share a key each move to `/{name}`,
  keeping method and definer, until none can move. A key still shared answers
  `503 route_collision`. The gateway logs the moved tools.
- **PA-7** Every tool re-list and config reload rebuilds the map atomically. A tool the first
  build moved stays at `/{name}` unless a `bindings` entry returns it. A key shared in a
  rebuild answers `503 route_collision` until a rebuild finds it unshared.
- **PA-8** Each bound key is pinned to its tool. If a rebuild resolves a pinned key to a
  different tool, the key answers `503 binding_changed`, naming both tools, until the operator
  confirms in the destination's config:

    ```json
    "bindings": {"POST /email CREATE": "add_email"}
    ```

    The key string is `METHOD /path`, plus a space and the definer on `POST`. A pinned key
    whose tool is gone gets the new map's `404` or `405`, never another tool.

## Allow and refusals (PA-9, PA-10)

- **PA-9** `Allow` lists the methods mapped on the path whose tool MB-1 currently admits, plus
  `HEAD` and `OPTIONS`, in the order `HEAD, OPTIONS, GET, POST, PUT, PATCH, DELETE`. A closed
  (`503`) key still counts as mapped.
- **PA-10** Resolution order for `GET`, `POST`, `PUT`, `PATCH`, `DELETE`:
    1. the path maps nothing: `404 tool_not_found`;
    2. the path does not map the method: `405 method_not_allowed` with `Allow`;
    3. on `POST`, the definer: `400 missing_definer`, `400 unknown_definer`,
       `400 definer_family_mismatch`, or `400 unknown_action` when the verb maps no tool on
       the path;
    4. a closed key: `503 route_collision` or `503 binding_changed`;
    5. the tool's contract does not admit the method: `405 method_not_allowed`.

    Any other method, such as `TRACE`, is `405` with the path's `Allow`, or `404` if the path
    maps nothing. `HEAD` and `OPTIONS` stop after step 1.

## Arguments (AR-1 to AR-5)

| Rule | Check | Refusal |
|---|---|---|
| AR-1 | A query key appears at most once; encode lists as one JSON value | `400 duplicate_query_key` |
| AR-2 | A `GET` carries arguments only in the query; its body is empty | `400 body_not_allowed` |
| AR-3 | A query value whose schema type cannot be a string (`integer`, `number`, `boolean`, `array`, `object`) parses as strict JSON, as in `?limit=5&ids=["a","b"]` | `400 invalid_arguments` |
| AR-4 | A non-empty body is one strict-JSON object (no `NaN`, `Infinity`, or overflowing numbers) | `400 invalid_arguments` |
| AR-5 | An argument is not in both the query and the body | `400 invalid_arguments` |

The reference gateway does not yet refuse duplicate keys inside a JSON body (the last one
wins), and does not validate arguments against `inputSchema`.

## Real exchanges

These are real answers of the reference gateway (`gateway/`) in front of the demo server
(`gateway/examples/demo_server.py`) at level 0, abridged to the headers that matter. `/note`
maps `GET` to `read_note` and `DELETE` to `delete_note`.

```text
TRACE /note
Host: notes.localhost

→ 405 Method Not Allowed
Allow: HEAD, OPTIONS, GET, DELETE
Cache-Control: no-store

{"error": "method_not_allowed", "detail": "/note does not map TRACE; it allows HEAD, OPTIONS, GET, DELETE"}
```

```text
POST /send/email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND

{"id":"welcome","to":"ana@example.com"}

→ 404 Not Found
Cache-Control: no-store

{"error": "tool_not_found", "detail": "/send/email maps no tool"}
```

```text
DELETE /delete_note?id=welcome
Host: notes.localhost

→ 404 Not Found
Cache-Control: no-store

{"error": "tool_not_found", "detail": "/delete_note maps no tool"}
```

```text
GET /note?id=welcome&id=other
Host: notes.localhost

→ 400 Bad Request
Cache-Control: no-store

{"error": "duplicate_query_key", "detail": "Query keys must not repeat (id); encode lists as JSON."}
```

```text
GET /note?id=welcome
Host: Notes.localhost

→ 404 Not Found
Cache-Control: no-store

{"error": "invalid_host", "detail": "Host labels must be lowercase DNS labels."}
```

## What to check in code

**Clients, harnesses, shims**

- The path comes from the route map (`OPTIONS /` or `GET /`, see `service-discovery`), not
  from the tool name. A tool keeps `/{name}` when its first word is not in the table, when the
  table's method is not admissible for its contract, or when its entry collided. Flag code
  that builds `/{tool_name}` or `/{verb}/{noun}` paths.
- One path segment. Ids and other arguments go in the query or the body (PA-1).
- On `GET`, arguments go only in the query, and the body is empty (AR-2).
- Each query key once; non-string values as strict JSON (AR-1, AR-3); no argument in both
  query and body (AR-5).
- The destination is a lowercase label; tokens and keys are per destination (HG-2, HG-6).
- A `503 route_collision` or `503 binding_changed` is an operator matter; do not retry it as
  a transient error.

**MCP servers whose tools a gateway will route**

- Name tools `verb_noun` with a verb from the table to get a noun path (`read_note` → `/note`).
- Annotate them so that the verb's method is admissible: `get_x` needs `readOnlyHint: true`
  to be `GET /x`; `delete_x` must be destructive (the default) and declare
  `idempotentHint: true` to be `DELETE /x`. Without annotations the strict defaults apply
  (TC-2), and both fall back to `POST /{name}` with `INVOKE`.
- Two tools that would share a key (`create_email` and `add_email`, both `POST /email` with
  `CREATE`) move to their full names at the first build, or close the key in a rebuild.

**Gateway and proxy config**

- A destination served on a public domain runs at level 1 or higher (HG-7).
- `labels` lists only qualifiers you mean to publish; the reference gateway refuses them all
  (HG-5).
- `bindings` keys are exact strings: `METHOD /path` and, on `POST`, ` DEFINER` (PA-8).

## Common issues

| Issue | Rule | Fix |
|---|---|---|
| Path built from the full tool name, such as `/delete_note` | PA-4 | Use the entry from the route map (`DELETE /note`) |
| Multi-segment path, such as `/notes/welcome` or `/send/email` | PA-1 | One segment; put the id in the query or body |
| Body on a `GET` | AR-2 | Move arguments to the query |
| Repeated query key for a list | AR-1 | One key with a JSON array value |
| Uppercase or underscore in a host label | HG-2 | A lowercase DNS label, as the gateway derives it (`MCP_DOCKER` → `mcp-docker`) |
| Level-0 destination on a public domain | HG-7 | Raise it to level 1 or serve it only on loopback |
| Retrying a `503 binding_changed` | PA-8 | The operator confirms the change in `bindings` |
