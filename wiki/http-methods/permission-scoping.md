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
| `POST:/message.slack` | Send Slack messages only |
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
  - "GET:/message.*"
  - "POST:/message.slack"
```

| Request | Check | Result |
|---|---|---|
| `GET /message.slack/123` | GET matches, path matches | Allowed |
| `GET /message.email/456` | GET matches, path matches | Allowed |
| `POST /message.slack` | POST matches, path matches | Allowed |
| `POST /message.email` | POST matches, path doesn't match | Denied |
| `DELETE /message.slack/123` | DELETE not in scopes | Denied |
| `GET /file.gdrive/abc` | path doesn't match /message.\* | Denied |

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
  - "GET:/message.slack"
  - "POST:/message.slack"
```

Services can reject overly broad scope requests.
