---
name: Subdomain Architecture
description: This skill should be used when working with WebSpec subdomain isolation, cookie/token scoping, origin security, the local.gimme.tools bridge, or local service architecture. Trigger phrases include "subdomain isolation", "cookie scope", "local.gimme.tools", "token audience", "origin security", "same-origin policy", "local bridge".
version: 1.0.0
---

# WebSpec Subdomain Architecture

> **Domain Note**: This skill uses `gimme.tools` as the default WebSpec domain. For self-hosted or enterprise deployments, substitute your configured domain.

## Overview

WebSpec inherits the browser's same-origin policy to create natural security boundaries. Each subdomain operates as an isolated security context, with cookies and tokens scoped to prevent cross-service access.

## Subdomain Isolation Model

### Trust Boundaries

```
┌─────────────────────────────────────────────────────────────────┐
│                        gimme.tools                               │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐│
│  │   auth.     │ │   api.      │ │   slack.    │ │   notion.   ││
│  │             │ │             │ │             │ │             ││
│  │ Session     │ │ Gateway     │ │ Slack API   │ │ Notion API  ││
│  │ management  │ │ LLM routing │ │ proxy       │ │ proxy       ││
│  │             │ │             │ │             │ │             ││
│  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘│
│         ↑               ↑               ↑               ↑       │
│         │               │               │               │       │
│    ┌────┴───────────────┴───────────────┴───────────────┴────┐  │
│    │                    Same-Origin Policy                    │  │
│    │    Cookies and tokens scoped to each subdomain          │  │
│    └─────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                      local.gimme.tools                       ││
│  │     WebSocket tunnel to localhost services                   ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

### Special Subdomains

| Subdomain | Purpose | Trust Level |
|-----------|---------|-------------|
| `auth.gimme.tools` | Session management, OAuth flows | Highest (root of trust) |
| `api.gimme.tools` | Gateway, LLM routing, service discovery | High |
| `local.gimme.tools` | Bridge to localhost services | Local trust |
| `{service}.gimme.tools` | Service-specific API proxy | Scoped to service |

### Subdomain Rules

1. **Each subdomain is isolated**: Cookies set on `slack.gimme.tools` cannot be read by `notion.gimme.tools`
2. **Root domain cookies are forbidden**: No cookies on bare `gimme.tools`
3. **Tokens are audience-bound**: JWT `aud` claim must match subdomain
4. **Cross-origin requires explicit CORS**: Services can't call each other without permission

## Cookie & Token Scoping

### Cookie Configuration

```
Set-Cookie: session_token=xxx;
  Domain=auth.gimme.tools;
  Path=/;
  HttpOnly;
  Secure;
  SameSite=Strict;
  Max-Age=604800
```

**Critical cookie attributes:**

| Attribute | Value | Purpose |
|-----------|-------|---------|
| `Domain` | Specific subdomain | Prevents cross-service access |
| `HttpOnly` | true | Prevents JavaScript access |
| `Secure` | true | HTTPS only |
| `SameSite` | Strict | Prevents CSRF |
| `Path` | / | Full subdomain scope |

### Cookie Scoping Rules

```
❌ WRONG: Cookies on root domain
Set-Cookie: token=xxx; Domain=gimme.tools

✅ CORRECT: Cookies on specific subdomain
Set-Cookie: token=xxx; Domain=auth.gimme.tools
```

### Token Audience Binding

Every JWT must include an `aud` (audience) claim matching the subdomain:

```json
{
  "iss": "auth.gimme.tools",
  "sub": "user_12345",
  "aud": "slack.gimme.tools",
  "exp": 1704067200,
  "scope": "GET:channels/*,POST:channels/*/messages",
  "session_id": "sess_abc123",
  "device_id": "dev_xyz789"
}
```

### Token Validation

```javascript
function validateToken(token, request) {
  const payload = decodeJWT(token);
  const requestSubdomain = new URL(request.url).hostname;

  // Audience must match request subdomain
  if (payload.aud !== requestSubdomain) {
    return { valid: false, error: 'Token audience mismatch' };
  }

  // Check expiration
  if (Date.now() > payload.exp * 1000) {
    return { valid: false, error: 'Token expired' };
  }

  // Check scope covers requested action
  if (!scopeCovers(payload.scope, request)) {
    return { valid: false, error: 'Insufficient scope' };
  }

  return { valid: true, payload };
}
```

## local.gimme.tools Bridge

### Architecture

The local bridge provides secure access to localhost services through a WebSocket tunnel:

```
┌───────────────────────────────────────────────────────────────┐
│                         Browser/Agent                          │
│                              │                                 │
│                    HTTPS request to                            │
│                  local.gimme.tools/...                         │
│                              │                                 │
│                              ▼                                 │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │              local.gimme.tools (Cloud)                   │  │
│  │                                                          │  │
│  │   1. Validates session token                             │  │
│  │   2. Checks device binding                               │  │
│  │   3. Routes through WebSocket tunnel                     │  │
│  │                                                          │  │
│  └────────────────────────┬────────────────────────────────┘  │
│                           │                                    │
│                    WebSocket tunnel                            │
│                    (authenticated)                             │
│                           │                                    │
│                           ▼                                    │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │              Local Bridge Agent                          │  │
│  │              (runs on user's machine)                    │  │
│  │                                                          │  │
│  │   1. Validates tunnel authentication                     │  │
│  │   2. Maps to local Unix socket                           │  │
│  │   3. Returns response through tunnel                     │  │
│  │                                                          │  │
│  └────────────────────────┬────────────────────────────────┘  │
│                           │                                    │
│                    Unix socket                                 │
│                           │                                    │
│                           ▼                                    │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │              Local Service                               │  │
│  │              (filesystem, clipboard, etc.)               │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
```

### WebSocket Protocol

```javascript
// Client → Server: Request
{
  "type": "request",
  "id": "req_123",
  "method": "GET",
  "path": "/clipboard",
  "headers": { "Authorization": "Bearer ..." }
}

// Server → Client: Response
{
  "type": "response",
  "id": "req_123",
  "status": 200,
  "headers": { "Content-Type": "text/plain" },
  "body": "clipboard contents..."
}
```

### Device Binding

Local bridge requires device binding verification:

```json
{
  "aud": "local.gimme.tools",
  "device_id": "dev_xyz789",
  "device_verified": true,
  "local_scopes": ["clipboard:read", "filesystem:read"]
}
```

## Local Service Architecture

### Unix Socket Isolation

Local services communicate through Unix domain sockets for security:

```
/var/run/gimme/
├── clipboard.sock      # Clipboard access
├── filesystem.sock     # File operations
├── terminal.sock       # Terminal access
└── browser.sock        # Browser automation
```

**Benefits of Unix sockets:**
- No network exposure (localhost-only)
- File permission controls
- Fast IPC without network overhead
- Natural sandboxing

### Service Registration

```yaml
# /etc/gimme/services.d/clipboard.yaml
service:
  name: clipboard
  socket: /var/run/gimme/clipboard.sock
  capabilities:
    - clipboard:read
    - clipboard:write
  owner: _gimme
  mode: 0660
```

### Sandbox Configuration (macOS)

```
;; macOS sandbox profile for gimme services
(version 1)
(deny default)

;; Allow socket communication
(allow network* (local-socket (path "/var/run/gimme/")))

;; Allow clipboard access
(allow mach-lookup (global-name "com.apple.pasteboard.1"))

;; Deny network
(deny network-outbound)
(deny network-inbound)
```

### Sandbox Configuration (Linux/systemd)

```ini
# /etc/systemd/system/gimme-clipboard.service
[Service]
User=gimme
Group=gimme

# Filesystem isolation
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

# Network isolation
PrivateNetwork=true

# Socket activation
ListenStream=/var/run/gimme/clipboard.sock
```

## Security Validation Checklist

When reviewing subdomain and security configuration:

- [ ] Cookies are scoped to specific subdomains (not root)
- [ ] HttpOnly flag set on all session cookies
- [ ] SameSite=Strict for CSRF protection
- [ ] JWT `aud` claim matches target subdomain
- [ ] Token expiration is enforced
- [ ] Local services use Unix sockets (not TCP)
- [ ] Sandbox profiles limit service capabilities
- [ ] Device binding verified for local bridge access
- [ ] No cross-origin requests without explicit CORS
- [ ] Session tokens never exposed to JavaScript

## Common Security Issues

| Issue | Risk | Fix |
|-------|------|-----|
| Root domain cookies | Cross-service token theft | Scope to specific subdomain |
| Missing HttpOnly | XSS can steal tokens | Add HttpOnly flag |
| Audience mismatch | Token replay attacks | Validate aud claim |
| TCP local services | Network exposure | Use Unix sockets |
| Missing device binding | Unauthorized local access | Require device verification |
