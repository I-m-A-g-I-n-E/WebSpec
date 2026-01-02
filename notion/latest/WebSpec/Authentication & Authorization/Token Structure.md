# Token Structure

WebSpec tokens are JWTs with claims designed for the subdomain-scoped permission model.

## Token Types

| Type | Lifetime | Scope | Storage |
| --- | --- | --- | --- |
| **Session** | Days/weeks | Full user session | HttpOnly cookie @ [auth.gimme.tools](http://auth.gimme.tools) |
| **Service** | Minutes/hours | Single service + methods | Bearer header |
| **Refresh** | Weeks | Token renewal only | Secure storage |

---

## Service Token Structure

```json
{
  "header": {
    "alg": "ES256",
    "typ": "JWT",
    "kid": "key-2024-01"
  },
  "payload": {
    "iss": "[auth.gimme.tools](http://auth.gimme.tools)",
    "sub": "user-123",
    "aud": "[slack.gimme.tools](http://slack.gimme.tools)",
    "iat": 1702600000,
    "exp": 1702603600,
    "jti": "token-abc123",
    
    "scope": [
      "GET:slack.gimme.tools/messages/*",
      "POST:slack.gimme.tools/messages/*",
      "GET:slack.gimme.tools/files/*"
    ],
    
    "session_id": "sess-xyz789",
    "device_id": "device-abc123"
  }
}
```

---

## Claim Definitions

### Standard Claims

| Claim | Description |
| --- | --- |
| `iss` | Issuer. Always [`auth.gimme.tools`](http://auth.gimme.tools) |
| `sub` | Subject. User identifier |
| `aud` | Audience. **The subdomain this token is valid for** |
| `iat` | Issued at. Unix timestamp |
| `exp` | Expiration. Unix timestamp |
| `jti` | JWT ID. Unique token identifier for revocation |

### WebSpec Claims

| Claim | Description |
| --- | --- |
| `scope` | Array of `METHOD:host/path` patterns |
| `session_id` | Links to user's session for revocation |
| `device_id` | For [local.gimme.tools](http://local.gimme.tools), binds to physical device |

---

## Scope Patterns

Scope strings follow the format:

```
METHOD:host/path_pattern
```

### Pattern Syntax

| Pattern | Meaning |
| --- | --- |
| `*` | Matches any single path segment |
| `**` | Matches any path depth |
| `.*` | Matches any type suffix |

### Examples

```yaml
# Read any message type
"GET:slack.gimme.tools/messages/*"

# Send only text messages  
"POST:slack.gimme.tools/messages/text"

# All operations on files
"*:drive.google.gimme.tools/files/**"

# Read specific message
"GET:slack.gimme.tools/messages/abc123"

# Linear tasks with specific prefix
"*:linear.gimme.tools/issues/LIN-*"
```

---

## Audience Binding

The `aud` claim is the security linchpin:

```
Token issued for: [slack.gimme.tools](http://slack.gimme.tools)
Token aud claim:  "[slack.gimme.tools](http://slack.gimme.tools)"

Presented to [slack.gimme.tools](http://slack.gimme.tools):
  → aud === host ✓
  → Token accepted

Presented to [notion.gimme.tools](http://notion.gimme.tools):
  → aud !== host ✗
  → Token rejected (403)

Intercepted and replayed:
  → Only works at intended service
  → Useless elsewhere
```

---

## Token Validation Algorithm

```python
def validate_token(token: str, request: Request) -> User:
    # 1. Decode and verify signature
    claims = jwt.decode(
        token,
        public_key,
        algorithms=["ES256"],
        issuer="[auth.gimme.tools](http://auth.gimme.tools)"
    )
    
    # 2. Check audience matches request host
    if claims["aud"] != [request.host](http://request.host):
        raise InvalidAudience(
            f"Token for {claims['aud']}, got {[request.host](http://request.host)}"
        )
    
    # 3. Check expiration
    if claims["exp"] < [time.now](http://time.now)():
        raise TokenExpired()
    
    # 4. Check scope covers this request
    request_pattern = f"{request.method}:{[request.host](http://request.host)}{request.path}"
    
    if not any(matches(request_pattern, s) for s in claims["scope"]):
        raise InsufficientScope(
            f"{request_pattern} not in {claims['scope']}"
        )
    
    # 5. Optional: Check token not revoked
    if is_revoked(claims["jti"]):
        raise TokenRevoked()
    
    return User(id=claims["sub"])
```

---

## Local Bridge Token

Tokens for [`local.gimme.tools`](http://local.gimme.tools) include device binding:

```json
{
  "aud": "local.gimme.tools",
  "scope": [
    "GET:local.gimme.tools/files/*",
    "POST:local.gimme.tools/code/*"
  ],
  "device_id": "device-abc123",
  "device_name": "Preston's MacBook"
}
```

Validation adds:

```python
# Check device is connected
if claims["device_id"] not in active_tunnels:
    raise DeviceNotConnected()

# Check user owns device
if not user_owns_device(claims["sub"], claims["device_id"]):
    raise DeviceNotOwned()
```

---

## Token Lifecycle

```
                 ┌─────────────────┐
                 │  [auth.gimme.tools](http://auth.gimme.tools) │
                 └────────┬────────┘
                          │
    ┌────────────────────┼────────────────────┐
    ▼                    ▼                    ▼
┌─────────┐      ┌───────────┐      ┌─────────┐
│  ISSUE   │      │  VALIDATE  │      │  REVOKE  │
└────┬────┘      └─────┬─────┘      └────┬────┘
     │                  │                  │
     ▼                  ▼                  ▼
┌─────────┐      ┌───────────┐      ┌─────────┐
│ Session  │      │ Signature  │      │ Blacklist│
│ cookie   │      │ + claims   │      │ by jti  │
│ valid    │      │ check      │      │          │
└─────────┘      └───────────┘      └─────────┘
```

---

## Security Properties

| Property | How Achieved |
| --- | --- |
| Confidentiality | HTTPS only, short lifetime |
| Integrity | ES256 signature |
| Audience restriction | `aud` claim, validated by each service |
| Scope limitation | `scope` array, pattern matching |
| Revocability | `jti` claim, blacklist check |
| Session binding | `session_id` claim |
| Device binding | `device_id` claim (for local) |