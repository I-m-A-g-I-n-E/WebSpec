# Onboarding & Subdomain Provisioning

WebSpec provides a graceful onboarding path that maintains security guarantees while accommodating real-world provisioning latency.

## The Problem

Subdomain provisioning isn't instant:

- TLS certificates take time to issue
- DNS propagation has inherent delays
- User verification may require async steps

Blocking all access until provisioning completes creates friction. But the solution isn't to restrict operations—it's to **consolidate them correctly once the subdomain is ready**.

---

## The Temporary License Model

WebSpec uses a pattern familiar to anyone who's visited the DMV: **temporary credentials that work fully until the real ones arrive, then become void**.

| Phase | Endpoint | Allowed Operations |
| --- | --- | --- |
| **Provisioning** | [`api.gimme.tools/userid/](http://api.gimme.tools/userid/)...` | All operations (authenticated to user) |
| **Active** | [`userid.gimme.tools/](http://userid.gimme.tools/)...` | All operations per user-configured permissions |

The key insight: **provisioning is full-access; activation is full-isolation**.

---

## How It Works

### During Provisioning

```jsx
// User just signed up, certificate pending
GET [api.gimme.tools/user-123/profile](http://api.gimme.tools/user-123/profile)       ✅ Allowed
POST [api.gimme.tools/user-123/messages](http://api.gimme.tools/user-123/messages)     ✅ Allowed
DELETE [api.gimme.tools/user-123/drafts/5](http://api.gimme.tools/user-123/drafts/5)   ✅ Allowed
PUT [api.gimme.tools/user-123/settings](http://api.gimme.tools/user-123/settings)      ✅ Allowed
```

Users have full access to their data via path-based URLs. This enables:

- **Bulk operations** without certificate overhead
- **Immediate productivity** from signup
- **Simpler client logic** during onboarding

All operations are still authenticated to the user—the path includes the user ID and tokens are scoped appropriately.

### After Subdomain Is Live

```jsx
// Certificate issued, subdomain active
GET [api.gimme.tools/user-123/profile](http://api.gimme.tools/user-123/profile)       ❌ 301 → [user-123.gimme.tools/profile](http://user-123.gimme.tools/profile)
POST [api.gimme.tools/user-123/messages](http://api.gimme.tools/user-123/messages)     ❌ 301 → [user-123.gimme.tools/messages](http://user-123.gimme.tools/messages)

GET [user-123.gimme.tools/profile](http://user-123.gimme.tools/profile)           ✅ Allowed (per user config)
POST [user-123.gimme.tools/messages](http://user-123.gimme.tools/messages)         ✅ Allowed (per user config)
```

The path-based route **permanently redirects** to the subdomain. No exceptions. The temporary license is void.

---

## Gateway Enforcement

The gateway performs an atomic check on every path-based request:

```python
def handle_path_based_request(user_id, request):
    # Check if user's subdomain is live
    if subdomain_is_active(f"{user_id}.[gimme.tools](http://gimme.tools)"):
        # Subdomain exists - permanent redirect, no exceptions
        return redirect(
            f"https://{user_id}.[gimme.tools](http://gimme.tools){request.path}",
            status=301  # Permanent redirect
        )
    
    # Subdomain not yet active - allow full access
    if not user_authenticated(request, user_id):
        return error(401, "Authentication required")
    
    return process_request(request)
```

### Key Properties

- **Atomic check**: No race conditions between provisioning and access
- **301 Permanent Redirect**: Teaches clients to update their URLs; cacheable
- **No lingering paths**: Once subdomain is live, the path route is dead forever
- **Full access during provisioning**: No artificial restrictions on write operations

---

## User-Configurable Permission Schema

Once the subdomain is active, users control their own permission model:

### Public Discovery (No Auth)

```yaml
# What can anonymous requests discover?
public:
  HEAD: true          # Existence checks
  OPTIONS: true       # Capability discovery
  GET: false          # No anonymous reads by default
```

### Authenticated Access

```yaml
# What requires authentication?
authenticated:
  GET: true           # Read own data
  POST: true          # Create new resources
  PATCH: true         # Update resources
  PUT: true           # Replace resources
  DELETE: true        # Remove resources
```

### OAuth Application Grants

```yaml
# What can third-party apps do on your behalf?
oauth_apps:
  default_grants:
    - "GET:/profile"
    - "GET:/messages/*"
  require_approval:
    - "POST:*"
    - "DELETE:*"
  notify_on_request: true
```

This becomes a **universal, user-owned permission schema**:

- Fully auditable
- Syntactically consistent (`METHOD:path`)
- User sets defaults they're comfortable with
- System notifies on requests beyond defaults

---

## Security Rationale

### Why Full Access Is Safe During Provisioning

| Concern | Mitigation |
| --- | --- |
| Cross-user data access | Path includes user ID; gateway enforces ownership |
| Token scope creep | Tokens audience-bound to [`api.gimme.tools`](http://api.gimme.tools), scoped to user |
| Session hijacking | Session cookies remain on [`auth.gimme.tools`](http://auth.gimme.tools) only |
| Unauthorized operations | User must be authenticated; operations are on their own data |

### Why Subdomain Cutover Is Non-Negotiable

Once the subdomain is live, path-based access dies because:

- **Same-origin isolation kicks in**: Cookie scoping, localStorage isolation, CSRF protection
- **User-configured permissions take effect**: The user now controls their access model
- **Blast radius shrinks**: Compromise of one subdomain doesn't affect [`api.gimme.tools`](http://api.gimme.tools) or other users
- **Audit trail clarity**: All operations happen at a single, user-owned origin

---

## Client Implementation

Clients should handle the transition gracefully:

```jsx
async function apiCall(userId, path, options = {}) {
    // Check cache for known subdomain
    const cachedSubdomain = getSubdomainCache(userId);
    if (cachedSubdomain) {
        return fetch(`[https://${cachedSubdomain}${path}`](https://${cachedSubdomain}${path}`), options);
    }
    
    // Try path-based URL (works during provisioning)
    const response = await fetch(
        `[https://api.gimme.tools/${userId}${path}`](https://api.gimme.tools/${userId}${path}`),
        { ...options, redirect: 'manual' }
    );
    
    // If 301, subdomain is now active - cache and follow
    if (response.status === 301) {
        const location = response.headers.get('Location');
        const subdomain = new URL(location).host;
        setSubdomainCache(userId, subdomain);
        return fetch(location, options);
    }
    
    return response;
}
```

---

## Provisioning Status API

Clients can check provisioning status:

```bash
GET [api.gimme.tools/user-123/.well-known/subdomain-status](http://api.gimme.tools/user-123/.well-known/subdomain-status)
```

```json
{
  "user_id": "user-123",
  "subdomain": "[user-123.gimme.tools](http://user-123.gimme.tools)",
  "status": "provisioning",
  "certificate": {
    "status": "pending",
    "estimated_ready": "2025-01-15T10:30:00Z"
  },
  "current_access": {
    "endpoint": "[api.gimme.tools/user-123/](http://api.gimme.tools/user-123/)",
    "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
  }
}
```

Once active:

```json
{
  "user_id": "user-123",
  "subdomain": "[user-123.gimme.tools](http://user-123.gimme.tools)",
  "status": "active",
  "certificate": {
    "status": "issued",
    "expires": "2026-01-15T10:30:00Z"
  },
  "current_access": {
    "endpoint": "[user-123.gimme.tools/](http://user-123.gimme.tools/)",
    "methods": "per user configuration"
  }
}
```

---

## Why This Matters

The temporary license pattern embodies a core WebSpec principle: **make the secure path the easy path**.

- Users aren't blocked waiting for certificates
- Full functionality from day one
- Transition is automatic and invisible
- No legacy endpoints linger after provisioning
- Users own their permission model post-activation

Friction breeds shortcuts. By eliminating onboarding friction *within* the security model—and then graduating users to a self-sovereign permission schema—WebSpec removes the temptation to work around it.