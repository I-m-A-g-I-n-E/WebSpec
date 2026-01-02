# Cookie & Token Scoping

Cookies and tokens are scoped to minimize exposure and blast radius.

## Cookie Architecture

### The Wrong Way

```
# Cookie on apex domain - DANGEROUS
Set-Cookie: session=xxx; Domain=.[gimme.tools](http://gimme.tools); HttpOnly; Secure
```

Problems:

- Every subdomain can read this cookie
- Compromised service = compromised session
- No isolation between services

### The Right Way

```
# Cookie on auth subdomain ONLY
Set-Cookie: session=xxx; Domain=[auth.gimme.tools](http://auth.gimme.tools); HttpOnly; Secure; SameSite=Strict
```

Benefits:

- Only [`auth.gimme.tools`](http://auth.gimme.tools) can read the session cookie
- Services must call [`auth.gimme.tools/verify`](http://auth.gimme.tools/verify) to validate
- Services never see the actual session token
- Compromised service ≠ compromised session

---

## Token Flow

```
┌──────────────┐      ┌──────────────────┐      ┌──────────────────┐
│    Client    │      │ [auth.gimme.tools](http://auth.gimme.tools) │      │ [slack.gimme.tools](http://slack.gimme.tools)│
└──────┬───────┘      └────────┬─────────┘      └────────┬─────────┘
       │                       │                       │
       │  1. Login request      │                       │
       │──────────────────────▶│                       │
       │                       │                       │
       │  2. Session cookie     │                       │
       │◀──────────────────────│                       │
       │  (HttpOnly, scoped     │                       │
       │   to [auth.gimme.tools](http://auth.gimme.tools)) │                       │
       │                       │                       │
       │  3. Request scoped token for Slack            │
       │──────────────────────▶│                       │
       │                       │                       │
       │  4. Scoped service token                      │
       │◀──────────────────────│                       │
       │  (aud: [slack.gimme.tools](http://slack.gimme.tools),                     │
       │   short-lived)        │                       │
       │                       │                       │
       │  5. API request with scoped token             │
       │───────────────────────────────────────────────▶│
       │                       │                       │
       │  6. Response           │                       │
       │◀───────────────────────────────────────────────│
```

---

## Token Scoping Layers

### Session Token (Master)

```yaml
location: [auth.gimme.tools](http://auth.gimme.tools) only (HttpOnly cookie)
lifetime: days/weeks
scope: full user session
visibility: never leaves auth subdomain
```

### Service Token (Scoped)

```yaml
location: passed in Authorization header
lifetime: minutes/hours
scope: specific service + methods
visibility: only valid at intended service
```

### Structure

```json
{
  "sub": "user-123",
  "aud": "[slack.gimme.tools](http://slack.gimme.tools)",
  "scope": [
    "GET:[slack.gimme.tools/message.*](http://slack.gimme.tools/message.*)",
    "POST:[slack.gimme.tools/message.*](http://slack.gimme.tools/message.*)"
  ],
  "iat": 1702600000,
  "exp": 1702603600
}
```

Key claims:

- `aud` (audience): The ONLY service that can accept this token
- `scope`: Exactly which methods and paths are allowed
- `exp`: Short expiration (1 hour typical)

---

## Audience Binding

The `aud` claim is the subdomain. Tokens are bound to their destination.

```yaml
Token issued for: [slack.gimme.tools](http://slack.gimme.tools)
Token contains:   aud: "[slack.gimme.tools](http://slack.gimme.tools)"

Scenarios:
  - Presented to [slack.gimme.tools](http://slack.gimme.tools)  → ✅ aud matches, accepted
  - Presented to [notion.gimme.tools](http://notion.gimme.tools) → ❌ aud mismatch, rejected
  - Intercepted and replayed        → ❌ only works at intended service
```

Even if a token leaks, it's useless outside its intended service.

---

## Blast Radius Containment

| Compromise | Without Scoping | With Scoping |
| --- | --- | --- |
| Leaked service token | Access to everything | Access to one service, limited time |
| Compromised service | Can read all user data | Can only see own scoped data |
| XSS on service subdomain | Full session hijack | Limited to that service |
| Cookie theft | Full account access | Nothing (HttpOnly + auth scope) |

---

## Token Lifecycle

```
1. User authenticates at [auth.gimme.tools](http://auth.gimme.tools)
   → Session cookie set (HttpOnly, auth subdomain only)

2. User wants to use Slack
   → Request service token from [auth.gimme.tools](http://auth.gimme.tools)
   → Auth verifies session cookie
   → Auth checks user has connected Slack
   → Auth issues scoped token (aud: [slack.gimme.tools](http://slack.gimme.tools))

3. Client uses Slack
   → Request to [slack.gimme.tools](http://slack.gimme.tools) with Bearer token
   → Slack verifies aud === self
   → Slack checks scope covers requested method+path
   → Request proceeds or fails

4. Token expires
   → Client requests new token from [auth.gimme.tools](http://auth.gimme.tools)
   → Cycle repeats
```

---

## Implementation Notes

### Cookie Attributes

```
Set-Cookie: session=xxx;
  Domain=[auth.gimme.tools](http://auth.gimme.tools);
  Path=/;
  HttpOnly;
  Secure;
  SameSite=Strict;
  Max-Age=604800
```

- `HttpOnly`: JavaScript cannot read it
- `Secure`: HTTPS only
- `SameSite=Strict`: No cross-site requests
- `Domain`: Only auth subdomain

### Token Validation

```python
def validate_token(token, request):
    claims = jwt.decode(token, public_key)
    
    # Check audience
    if claims['aud'] != [request.host](http://request.host):
        raise InvalidAudience()
    
    # Check expiration
    if claims['exp'] < [time.now](http://time.now)():
        raise TokenExpired()
    
    # Check scope
    pattern = f"{request.method}:{[request.host](http://request.host)}{request.path}"
    if not any(fnmatch(pattern, s) for s in claims['scope']):
        raise InsufficientScope()
    
    return claims['sub']  # Return user ID
```