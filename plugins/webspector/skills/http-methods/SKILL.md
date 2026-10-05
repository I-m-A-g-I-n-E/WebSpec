---
name: HTTP Methods
description: This skill should be used when working with HTTP method semantics, permission scoping, verb absorption, idempotency guarantees, HEAD/OPTIONS discovery, or METHOD tokenization for prompt injection defense. Trigger phrases include "http method", "permission scope", "method tokenization", "verb mapping", "idempotent", "rest semantics".
version: 1.0.0
---

# WebSpec HTTP Methods

> **Domain Note**: This skill uses `gimme.tools` as the default WebSpec domain. For self-hosted or enterprise deployments, substitute your configured domain.

## Overview

WebSpec leverages HTTP methods as semantic verbs, enabling intuitive permission scoping and prompt injection defense. Each method represents a specific action category with absorbed natural language verbs.

## Method Semantics

### Core Method Mapping

| Method | Semantic | Primary Action |
|--------|----------|----------------|
| GET | read | Retrieve resources without modification |
| POST | create | Create new resources |
| PUT | replace | Complete replacement of a resource |
| PATCH | update | Partial modification of a resource |
| DELETE | remove | Delete or archive resources |
| HEAD | probe | Lightweight existence/metadata check |
| OPTIONS | discover | Capability and permission discovery |

### Absorbed Verb Lists

Each HTTP method absorbs natural language verbs that map to its semantic:

**GET (read)**
```
fetch, retrieve, list, show, view, display, get, load,
read, query, search, find, lookup, check, see
```

**POST (create)**
```
create, add, insert, new, submit, send, post, write,
compose, draft, generate, make, build, start
```

**PUT (replace)**
```
replace, set, overwrite, put, reset, initialize,
restore, revert
```

**PATCH (update)**
```
update, modify, change, edit, patch, adjust, tweak,
revise, amend, fix, correct
```

**DELETE (remove)**
```
delete, remove, trash, archive, destroy, purge,
clear, erase, drop, cancel, revoke
```

### Idempotency Guarantees

| Method | Idempotent | Safe | Description |
|--------|------------|------|-------------|
| GET | Yes | Yes | Multiple calls return same result |
| HEAD | Yes | Yes | Same as GET, no body |
| OPTIONS | Yes | Yes | Returns capabilities |
| PUT | Yes | No | Same input = same state |
| DELETE | Yes | No | Already deleted = no change |
| POST | No | No | Each call may create new resource |
| PATCH | No | No | May depend on current state |

## Permission Scoping

### Scope Grammar

```
SCOPE := METHOD ":" HOST? PATH_PATTERN

METHOD := "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "*"
HOST := subdomain "." domain
PATH_PATTERN := "/" segment { "/" segment }
segment := identifier | "*" | "**"
```

### Scope Examples

```bash
# Specific method + specific path
GET:slack.gimme.tools/channels/C123/messages

# Specific method + wildcard path
GET:slack.gimme.tools/channels/*

# All methods on specific path
*:gdrive.gimme.tools/files/shared/*

# Method on any provider
POST:*/messages

# Recursive wildcard (any depth)
GET:github.gimme.tools/repos/**/contents/**

# Multiple scopes (comma-separated)
GET:slack.gimme.tools/channels/*,POST:slack.gimme.tools/channels/*/messages
```

### Scope Hierarchy

Broader scopes implicitly grant narrower permissions:

```
*:slack.gimme.tools/**           # Full access to Slack
  └── GET:slack.gimme.tools/**   # Read-only access
      └── GET:slack.gimme.tools/channels/*  # Read channels only
          └── GET:slack.gimme.tools/channels/C123  # Read one channel
```

### Permission Inheritance

```javascript
function scopeMatches(scope, request) {
  const [scopeMethod, scopePath] = scope.split(':');
  const [reqMethod, reqPath] = [request.method, request.path];

  // Method check: "*" matches all
  if (scopeMethod !== '*' && scopeMethod !== reqMethod) {
    return false;
  }

  // Path check with wildcards
  return pathMatchesPattern(reqPath, scopePath);
}
```

## HEAD & OPTIONS Discovery

### HEAD for Lightweight Probing

HEAD returns headers without body, useful for:
- Checking resource existence
- Getting metadata (size, type, modified date)
- Validating permissions before full request

```bash
# Check if file exists and get metadata
HEAD gdrive.gimme.tools/files/abc123
→ 200 OK
  Content-Type: application/pdf
  Content-Length: 1048576
  Last-Modified: 2024-01-15T10:30:00Z

# Check if message exists
HEAD slack.gimme.tools/channels/C123/messages/M456
→ 200 OK (exists) or 404 Not Found
```

### OPTIONS for Capability Discovery

OPTIONS reveals what methods are allowed and what permissions exist:

```bash
OPTIONS slack.gimme.tools/channels/C123/messages
→ 200 OK
  Allow: GET, POST, HEAD, OPTIONS
  X-WebSpec-Scopes: GET:read, POST:create
  X-WebSpec-Rate-Limit: 100/minute
```

**Response Headers:**

| Header | Purpose |
|--------|---------|
| `Allow` | Permitted HTTP methods |
| `X-WebSpec-Scopes` | Required permission scopes |
| `X-WebSpec-Rate-Limit` | Rate limiting info |
| `X-WebSpec-Auth` | Authentication requirements |

## METHOD Tokenization (Prompt Injection Defense)

### The Security Problem

Natural language can embed dangerous verbs that might be interpreted as actions:

```
"Please read this message and delete the old files"
                              ^^^^^^
                         Dangerous verb!
```

### The Structural Solution

WebSpec uses METHOD tokenization to create a structural invariant:

**Rule**: Only ONE raw HTTP METHOD token may appear at the protocol level per request. All verbs within payloads must be tokenized.

### Tokenization Format

```
[M:METHOD]  or  [m:method]
```

Examples:
```
[M:DELETE]  →  Tokenized delete (safe in payload)
[M:GET]     →  Tokenized get (safe in payload)
[m:read]    →  Tokenized read (safe in payload)
```

### Before vs After Tokenization

**Unsafe payload:**
```json
{
  "instructions": "Delete all messages older than 30 days"
}
```

**Safe tokenized payload:**
```json
{
  "instructions": "[M:DELETE] all messages older than 30 days"
}
```

### Validation Algorithm

```javascript
function validateMethodTokenization(request) {
  const { method, body } = request;

  // Count raw METHOD tokens in body
  const rawMethods = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];
  const bodyText = JSON.stringify(body);

  for (const m of rawMethods) {
    // Raw method word (not tokenized)
    const rawPattern = new RegExp(`\\b${m}\\b(?![:\\]])`, 'gi');
    if (rawPattern.test(bodyText)) {
      return {
        valid: false,
        error: `Raw method "${m}" found in payload. Use [M:${m}] tokenization.`
      };
    }
  }

  return { valid: true };
}
```

### Why This Works

1. **Structural guarantee**: Parser only sees ONE real METHOD
2. **Detokenization at boundary**: Tokens restored when displaying to humans
3. **Defense in depth**: Even if LLM parses payload, tokenized verbs won't trigger actions
4. **Composable**: Works with any payload format (JSON, text, etc.)

## Method Selection Guidelines

### When to Use Each Method

| Scenario | Method | Example |
|----------|--------|---------|
| Fetching data | GET | `GET /channels/C123/messages` |
| Creating new resource | POST | `POST /channels/C123/messages` |
| Full resource replacement | PUT | `PUT /pages/xyz789` |
| Partial update | PATCH | `PATCH /issues/LIN-42` |
| Removing resource | DELETE | `DELETE /files/abc123` |
| Checking existence | HEAD | `HEAD /files/abc123` |
| Discovering capabilities | OPTIONS | `OPTIONS /channels/C123` |

### Common Mistakes

| Mistake | Problem | Correct Approach |
|---------|---------|------------------|
| POST for read | Non-idempotent for reads | Use GET |
| GET with body | Body ignored by spec | Use POST or query params |
| PUT for partial update | Overwrites entire resource | Use PATCH |
| DELETE for archive | Semantic mismatch | Use PATCH with status field |
| Ignoring idempotency | Duplicate side effects | Design for retry safety |

## Validation Checklist

When reviewing HTTP method usage:

- [ ] Method matches semantic intent (GET for read, POST for create, etc.)
- [ ] Idempotency requirements are met
- [ ] Permission scopes use correct method prefix
- [ ] Natural language verbs in payloads are tokenized
- [ ] HEAD used for lightweight probing where appropriate
- [ ] OPTIONS available for capability discovery
- [ ] No raw dangerous verbs in user-provided content
