# Permission Scoping

> **Status: Mixed.** The `METHOD:host/path` scope-matching mechanism itself is **Implemented (B)**
> — see `gateway/webspec/permissions.py` (`fnmatch`-based `METHOD:host/path` patterns). The
> examples below use the implementation's path form, where the path *is* the MCP tool name (e.g.
> `send_message`, `list_messages`) rather than an `object.provider` path segment (banned — see
> [Core Syntax](../url-grammar/core-syntax.md)). Examples with an `id`-style path segment (e.g.
> `/task/LIN-*`) illustrate the `/collection/id` REST hierarchy, which is **Proposed (C)** — see
> [status matrix](../index.md#status) and [ROADMAP-C.md](../ROADMAP-C.md).

Because HTTP methods carry semantic meaning, permissions become simple pattern matching on `METHOD:path`.

## The Core Insight

Traditional API permissions:

```yaml
# Custom vocabulary, varies per API
scopes:
  - messages:read
  - messages:write
  - messages:delete
  - files:read
  - files:write
```

Every API invents their own scope vocabulary. Parsers required.

WebSpec permissions:

```yaml
# Universal: METHOD:path pattern
scopes:
  - "GET:/message.*"
  - "POST:/message.*"
  - "DELETE:/message.*"
  - "GET:/file.*"
  - "POST:/file.*"
```

The permission IS the method + path. No custom vocabulary. Universal across all services.

---

## Scope Grammar

```
SCOPE := METHOD ":" HOST? PATH_PATTERN

METHOD := "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD" | "OPTIONS" | "*"
HOST := subdomain ".gimme.tools"
PATH_PATTERN := glob pattern with * and **
```

### Examples

| Scope | Meaning |
|---|---|
| `GET:/message.*` | Read any message type |
| `POST:slack.gimme.tools/send_message` | Send Slack messages only |
| `*:/file.*` | All operations on files |
| `GET:slack.gimme.tools/*` | Read anything from Slack |
| `DELETE:/task/LIN-*` | Delete Linear tasks only |
| `*:*` | Full access (dangerous) |

---

## Token Structure

```json
{
  "sub": "user-123",
  "aud": "slack.gimme.tools",
  "scope": [
    "GET:slack.gimme.tools/message.*",
    "GET:slack.gimme.tools/file.*",
    "POST:slack.gimme.tools/message.*"
  ],
  "exp": 1702600000
}
```

The `aud` (audience) claim binds the token to a specific subdomain. Even if the token leaks, it's rejected by other services.

---

## Gateway Enforcement

Authorization becomes trivial pattern matching:

```python
import fnmatch

def authorize(token, request):
    pattern = f"{request.method}:{request.host}{request.path}"
    return any(
        fnmatch.fnmatch(pattern, scope)
        for scope in token['scope']
    )
```

Six lines. Domain-agnostic. Works for any service.

---

## Enforcement Examples

```yaml
Token scopes:
  - "GET:*.gimme.tools/list_messages"
  - "POST:slack.gimme.tools/send_message"
```

| Request | Check | Result |
|---|---|---|
| `GET slack.gimme.tools/list_messages` | GET matches, host+path match `*.gimme.tools/list_messages` | Allowed |
| `GET email.gimme.tools/list_messages` | GET matches, host+path match `*.gimme.tools/list_messages` | Allowed |
| `POST slack.gimme.tools/send_message` | POST matches, host+path match `slack.gimme.tools/send_message` | Allowed |
| `POST email.gimme.tools/send_message` | POST matches, but host doesn't match `slack.gimme.tools` | Denied |
| `DELETE slack.gimme.tools/send_message` | DELETE not in scopes | Denied |
| `GET gdrive.gimme.tools/upload_file` | path doesn't match `list_messages` | Denied |

---

## Scope Hierarchies

### Read-only Access

```yaml
scopes:
  - "GET:*"      # Can read everything
  - "HEAD:*"     # Can check existence
  - "OPTIONS:*"  # Can discover capabilities
```

### Write Access (additive)

```yaml
scopes:
  - "POST:*"     # Can create
  - "PATCH:*"    # Can update
  - "PUT:*"      # Can replace
```

### Admin Access

```yaml
scopes:
  - "DELETE:*"   # Can remove
  - "*:*"        # Full access
```

---

## Scope Request UI

When connecting a service, users see exactly what's being granted:

```
+---------------------------------------------------+
|  Slack wants to:                                  |
+---------------------------------------------------+
|                                                   |
|  GET  /message.*    Read your messages             |
|  POST /message.*    Send messages                  |
|  GET  /file.*       Access shared files            |
|  DELETE /message.*  (not requested)                |
|                                                   |
|              [Authorize]  [Deny]                   |
+---------------------------------------------------+
```

The pattern is human-readable: users see exactly which methods on which paths are being requested.

---

## Least Privilege by Default

Clients should request minimum scopes:

```yaml
# Bad: requesting everything
scopes:
  - "*:*"

# Good: requesting exactly what's needed
scopes:
  - "GET:slack.gimme.tools/list_messages"
  - "POST:slack.gimme.tools/send_message"
```

Services can reject overly broad scope requests.
