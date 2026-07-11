# Method Semantics

> **Status: Mixed.** GET/POST/PUT/PATCH routing to MCP tools and the definer-verb tamper check are
> **Implemented (B)**. The concrete path examples below use the implementation's path form — the
> path *is* the MCP tool name (e.g. `send_message`, `create_task`) served under the provider's
> subdomain, not an `object.provider` path segment (banned — see
> [Core Syntax](../url-grammar/core-syntax.md)). Examples with a bare object-type path (e.g.
> `/message`) or an `id`-style segment (e.g. `/task/LIN-42`) illustrate the `/collection/id` REST
> hierarchy, which is **Proposed (C)** — see [status matrix](../index.md#status) and
> [ROADMAP-C.md](../ROADMAP-C.md).

HTTP methods are semantically rich but culturally underutilized. WebSpec restores their full meaning.

## The Problem

Most APIs reduce HTTP to two verbs:

```bash
# Typical API (verbs in path)
POST /api/messages/send
POST /api/messages/delete    # why POST?
POST /api/messages/update    # why POST?
GET  /api/messages/list
```

This wastes the semantic precision built into HTTP itself.

## The WebSpec Approach

The HTTP method IS the verb. The path is pure noun.

```bash
# WebSpec (verbs ARE methods)
POST   /message              # create/send
DELETE /message/123          # delete
PATCH  /message/123          # update
GET    /message              # list
GET    /message/123          # read one
```

---

## Method Mapping

### GET - Read Operations

Safe, idempotent retrieval. Never modifies state.

| Natural Language | WebSpec |
|---|---|
| "read my messages" | `GET /message` |
| "find files about Q3" | `GET /file?q=Q3` |
| "show me task LIN-42" | `GET /task/LIN-42` |
| "list calendar events" | `GET /event` |
| "search for Sarah" | `GET /contact?q=sarah` |

**Absorbed verbs:** read, get, fetch, retrieve, find, search, list, query, show, view

### POST - Create Operations

Non-idempotent creation or invocation.

| Natural Language | WebSpec |
|---|---|
| "send this to Slack" | `POST slack.gimme.tools/send_message` |
| "upload to Drive" | `POST gdrive.gimme.tools/upload_file` |
| "create a new task" | `POST linear.gimme.tools/create_task` |
| "schedule a meeting" | `POST gcal.gimme.tools/create_event` |
| "run this script" | `POST local.gimme.tools/execute_code` |

**Absorbed verbs:** create, send, post, upload, add, invoke, trigger, execute, run, submit

### PUT - Replace Operations

Idempotent full replacement.

| Natural Language | WebSpec |
|---|---|
| "replace the document" | `PUT /document/abc` |
| "overwrite the config" | `PUT /file/config.json` |
| "set my status" | `PUT /status` |

**Absorbed verbs:** replace, set, overwrite, reset

### PATCH - Modify Operations

Partial update without full replacement.

| Natural Language | WebSpec |
|---|---|
| "update the task title" | `PATCH /task/LIN-42` |
| "edit my message" | `PATCH /message/123` |
| "append to the doc" | `PATCH /document/abc?append=true` |
| "change my email" | `PATCH /profile` |

**Absorbed verbs:** update, modify, edit, change, patch, append, amend

### DELETE - Remove Operations

Removal or revocation.

| Natural Language | WebSpec |
|---|---|
| "delete that file" | `DELETE /file/abc` |
| "remove the task" | `DELETE /task/LIN-42` |
| "cancel the meeting" | `DELETE /event/xyz` |
| "revoke access" | `DELETE /permission/abc` |

**Absorbed verbs:** delete, remove, cancel, revoke, destroy, trash, archive

### HEAD - Metadata Operations

Like GET but returns headers only, no body.

| Natural Language | WebSpec |
|---|---|
| "does this file exist?" | `HEAD /file/abc` |
| "how big is this?" | `HEAD /file/abc` -> `Content-Length` |
| "when was this modified?" | `HEAD /doc/xyz` -> `Last-Modified` |
| "can I access this?" | `HEAD /file/abc` -> `200` or `403` |

**Absorbed verbs:** exists, check, metadata, stat, info

### OPTIONS - Discovery Operations

Discover what operations are possible.

| Natural Language | WebSpec |
|---|---|
| "what can I do with messages?" | `OPTIONS /message` |
| "what's available on Slack?" | `OPTIONS /` on slack.gimme.tools |

**Absorbed verbs:** discover, capabilities, help, what, how

---

## Idempotency Guarantees

| Method | Idempotent | Safe |
|---|---|---|
| GET | Yes | Yes |
| HEAD | Yes | Yes |
| OPTIONS | Yes | Yes |
| PUT | Yes | No |
| DELETE | Yes | No |
| POST | No | No |
| PATCH | No | No |

Idempotent = calling twice has same effect as calling once.
Safe = doesn't modify server state.

This enables:

- Safe retry on network failure for idempotent methods
- Caching for safe methods
- Clear expectations for clients

---

## Why This Matters for Permissions

Because the verb is the method, permissions become trivial patterns:

```yaml
# Old way: custom scopes
scopes:
  - messages:read
  - messages:write
  - messages:delete

# WebSpec way: method + path
scopes:
  - "GET:/message.*"
  - "POST:/message.*"
  - "DELETE:/message.*"
```

See [Permission Scoping](permission-scoping.md) for details.
