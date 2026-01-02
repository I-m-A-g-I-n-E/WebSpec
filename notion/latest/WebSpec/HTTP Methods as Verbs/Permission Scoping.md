# Permission Scoping

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
# WebSpec way: method + path
scopes:
  - "GET:*/messages/*"
  - "POST:*/messages/*"
  - "DELETE:*/messages/*"
  - "GET:*/files/*"
  - "POST:*/files/*"
```

The permission IS the method + path. No custom vocabulary. Universal across all services.

---

## Scope Grammar

```
SCOPE := METHOD ":" HOST? PATH_PATTERN

METHOD := "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD" | "OPTIONS" | "*"
HOST := subdomain ".[gimme.tools](http://gimme.tools)"
PATH_PATTERN := glob pattern with * and **
```

### Examples

| Scope | Meaning |
| --- | --- |
| `GET:*/messages/*` | Read any message type (global) |
| `POST:slack.gimme.tools/messages` | Send Slack messages only |
| `*:*/files/*` | All operations on files (global) |
| `GET:slack.gimme.tools/*` | Read anything from Slack |
| `DELETE:linear.gimme.tools/issues/LIN-*` | Delete Linear tasks only |
| `*:*` | Full access (dangerous) |

---

## Token Structure

```json
{
  "sub": "user-123",
  "aud": "slack.gimme.tools",
  "scope": [
    "GET:slack.gimme.tools/messages/*",
    "GET:slack.gimme.tools/files/*",
    "POST:slack.gimme.tools/messages"
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
    pattern = f"{request.method}:{[request.host](http://request.host)}{request.path}"
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
  - "GET:*/messages/*"
  - "POST:slack.gimme.tools/messages"
```

| Request | Check | Result |
| --- | --- | --- |
| `GET slack.gimme.tools/messages/123` | GET matches, path matches | ✅ Allowed |
| `GET gmail.gimme.tools/messages/456` | GET matches, path matches | ✅ Allowed |
| `POST slack.gimme.tools/messages` | POST matches, path matches | ✅ Allowed |
| `POST gmail.gimme.tools/messages` | POST matches, path doesn't match | ❌ Denied |
| `DELETE slack.gimme.tools/messages/123` | DELETE not in scopes | ❌ Denied |
| `GET gdrive.gimme.tools/files/abc` | path doesn't match /messages/* | ❌ Denied |

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
┌───────────────────────────────────────────────────┐
│  Slack wants to:                                │
├───────────────────────────────────────────────────┤
│                                                 │
│  ✅ GET  .../messages/*  Read your messages      │
│  ✅ POST .../messages    Send messages           │
│  ✅ GET  .../files/*     Access shared files     │
│  ❌ DELETE .../messages/* (not requested)        │
│                                                 │
│              [Authorize]  [Deny]                │
└───────────────────────────────────────────────────┘
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
  - "GET:slack.gimme.tools/messages/*"
  - "POST:slack.gimme.tools/messages"
```

Services can reject overly broad scope requests.