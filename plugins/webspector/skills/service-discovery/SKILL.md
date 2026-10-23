---
name: Service Discovery
description: This skill should be used when checking how WebSpec clients discover a destination's tools and route map - GET / (tools and their entries), OPTIONS / and OPTIONS /{noun} (per-method entries with inputSchema, contract, and requirements), HEAD / and HEAD /{noun} (health, Allow, Link), the reserved paths, and how a shim builds requests from discovery. Trigger phrases include "service discovery", "OPTIONS", "HEAD", "route map", "list tools", "inputSchema", "requirements", "service-desc", "WebSpec-Status".
version: 2.0.0
---

# WebSpec discovery

This skill follows the normative spec pages
[Methods and contracts](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/methods/#discovery)
(rules `DS-1` to `DS-3`), [Addressing](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/addressing/#path)
(`PA-2`), and [Security levels](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/levels/#the-harness-shim)
(`SH-2`). Cite a rule by its ID in every finding. Where this summary and the spec differ, the
spec wins.

A client never guesses a tool's method, path, or definer: it reads them from the destination's
route map.

## The discovery requests

| Request | Answers | Rule |
|---|---|---|
| `GET /` | The tools with their descriptions and their entries: method, path, and on `POST` the definer | DS-3 |
| `OPTIONS /` | Every entry of the route map, with its path | DS-1 |
| `OPTIONS /{noun}` | The path's `Allow`, and one entry per method mapped on the path (on `POST`, one per definer) | DS-1 |
| `HEAD /` | Health in `WebSpec-Status` (`connected` or `degraded`), `WebSpec-Tool-Count`, `WebSpec-Service` | DS-2 |
| `HEAD /{noun}` | `200` with `Allow` and `Link: </>; rel="service-desc"` (RFC 8631), or `404` if the path maps nothing | DS-2 |

- **DS-1** Each entry gives its `method` (and on `POST` its `definer`), the `tool` it resolves
  to, the tool's `inputSchema`, its `contract`, and the `requirements` at the destination's
  level: one boolean each for `guard`, `definer`, `empty_body`, `idempotency_key`,
  `clearance`, and `approval`. An entry whose key is closed names no tool and gives `error`:
  `route_collision` or `binding_changed`. A path that maps nothing is `404 tool_not_found`.
- **DS-2** `HEAD` never invokes a tool, so it reports the route, not whether an item exists.
  That departs from RFC 9110 §9.3.2.
- **DS-3** Every discovery response carries `Cache-Control: no-store`.
- `contract.source` is `annotations`, `default` (the server gave none), `override`, `pinned`
  (a join of different sources), or `strictest` (an invalid override).
- **PA-2** Reserved paths are answered before the route map: `GET /`, `HEAD /`, `OPTIONS /`,
  `GET /__nonce` (level 1 and up), and the retired `GET /__challenge` (`410` at level 1 and
  up).
- **SH-2** A shim learns the route map, and what to add to each request, from `OPTIONS /` or
  `OPTIONS /{noun}`. Only a level-4 approval needs more: the challenge in the `428` answer to
  a first attempt.

At level 1 and higher, discovery requests carry the guard like any other request.

## Real exchanges

These are real answers of the reference gateway (`gateway/`) in front of the demo server
(`gateway/examples/demo_server.py`) at level 0, abridged to the headers that matter.

```text
GET /
Host: notes.localhost

→ 200 OK
Cache-Control: no-store

{"service": "notes", "level": 0, "tools": [{"name": "read_note", "description": "Read a note.", "method": "GET", "path": "/note"}, {"name": "send_email", "description": "Email a note to someone (simulated: queued in an in-memory outbox).", "method": "POST", "path": "/email", "definer": "SEND"}, {"name": "delete_note", "description": "Delete a note.", "method": "DELETE", "path": "/note"}]}
```

```text
OPTIONS /email
Host: notes.localhost

→ 200 OK
Allow: HEAD, OPTIONS, POST
Cache-Control: no-store

{"service": "notes", "level": 0, "entries": [{"path": "/email", "method": "POST", "definer": "SEND", "tool": "send_email", "inputSchema": {"properties": {"id": {"type": "string"}, "to": {"type": "string"}}, "required": ["id", "to"], "type": "object"}, "contract": {"read_only": false, "destructive": false, "idempotent": false, "open_world": true, "tier": "sensitive", "source": "annotations"}, "requirements": {"guard": false, "definer": true, "idempotency_key": false, "empty_body": false, "clearance": false, "approval": false}}]}
```

```text
HEAD /note
Host: notes.localhost

→ 200 OK
Allow: HEAD, OPTIONS, GET, DELETE
Link: </>; rel="service-desc"
Cache-Control: no-store
```

```text
HEAD /
Host: notes.localhost

→ 200 OK
WebSpec-Service: notes
WebSpec-Level: 0
WebSpec-Tool-Count: 3
WebSpec-Status: connected
Cache-Control: no-store
```

## What to check in code

**Clients, harnesses, shims**

- Build each request from the route map: the entry's method, path, and on `POST` its
  definer. Flag code that derives paths from tool names or hard-codes `/{tool_name}`.
- Use `requirements` to decide what to add (guard, `Idempotency-Key`, clearance) and the
  entry's `tool` to bind a clearance (CL-1).
- Treat an entry with `error` as closed: requests on its key answer `503` until the operator
  acts (PA-7, PA-8).
- A client that keeps the map refreshes it after a `404`, `405`, `400 unknown_action`, or
  `503` on a key; the map changes when the server re-lists its tools or the config reloads.
- Do not use `HEAD /{noun}` to test whether an item exists (DS-2).
- Read health from `WebSpec-Status`, not `X-WebSpec-Status`.

**Gateways**

- `OPTIONS` and `HEAD` never invoke a tool, and both are answered even when a key on the path
  is closed (MB-3, PA-10).
- Every discovery answer and every refusal carries `Cache-Control: no-store` (DS-3, RS-4).

## Known limits

Discovery that is answered and nonce issuance are not audited; discovery the guard refuses is.
`GET /` on the bare domain lists every
destination with its level, without the guard; the reference proxies answer `421` for the bare
domain, so it is reachable only from the host.

## Not part of WebSpec

Earlier drafts of this plugin described natural-language intent extraction, embedding
schemas, and a "three-way join" over connected services, a registry, and keychain hints. None
of these is in the spec. The repository's `gateway/webspec_registry` is an experimental tool
catalog outside the spec that ranks tools by keyword overlap; review it as ordinary code, not
against WebSpec rules.
