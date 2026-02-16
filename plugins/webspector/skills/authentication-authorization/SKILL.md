---
name: Authentication & Authorization
description: This skill should be used when working with WebSpec OAuth flows, JWT token structure, session management, keychain integration, device binding, or onboarding flows. Trigger phrases include "webspec auth", "token structure", "oauth flow", "jwt claims", "keychain integration", "session token", "device binding".
version: 1.0.0
---

# WebSpec Authentication & Authorization

> **Domain Note**: This skill uses `gimme.tools` as the default WebSpec domain. For self-hosted or enterprise deployments, substitute your configured domain.

## Overview

WebSpec implements a three-layer authorization model with OAuth 2.0 + PKCE for service connections. The system integrates with device keychains for zero-friction discovery and supports progressive authorization.

## Three Authorization Layers

```
┌─────────────────────────────────────────────────────────────────┐
│                    PLATFORM AUTHORIZATION                        │
│   User has a gimme.tools account and active platform session     │
│   Token: Platform session token (days)                           │
├─────────────────────────────────────────────────────────────────┤
│                    SESSION AUTHORIZATION                         │
│   User has connected a specific service (e.g., Slack)            │
│   Token: Service connection token (weeks, refreshable)           │
├─────────────────────────────────────────────────────────────────┤
│                   INVOCATION AUTHORIZATION                       │
│   User has granted permission for specific action                │
│   Token: Short-lived invocation token (minutes)                  │
└─────────────────────────────────────────────────────────────────┘
```

### Layer Details

| Layer | Scope | Token Lifetime | Refresh |
|-------|-------|----------------|---------|
| Platform | gimme.tools account | 7 days | Yes |
| Session | Service connection | 30 days | Yes |
| Invocation | Specific action | 5 minutes | No |

## OAuth 2.0 with PKCE Flow

### Authorization Request

```
GET https://auth.gimme.tools/oauth/authorize
  ?client_id=gimme_client
  &redirect_uri=https://app.gimme.tools/callback
  &response_type=code
  &scope=slack:channels:read slack:messages:write
  &state=random_state_value
  &code_challenge=base64url(sha256(code_verifier))
  &code_challenge_method=S256
```

### Authorization Flow

```
┌────────────┐     ┌────────────────┐     ┌────────────────┐
│   Agent    │     │ auth.gimme.tools│     │  Service OAuth │
│            │     │                 │     │  (e.g., Slack) │
└─────┬──────┘     └────────┬───────┘     └────────┬───────┘
      │                      │                      │
      │ 1. Request auth      │                      │
      │─────────────────────>│                      │
      │                      │                      │
      │ 2. Redirect to       │                      │
      │    service OAuth     │                      │
      │<─────────────────────│                      │
      │                      │                      │
      │ 3. User authorizes   │                      │
      │─────────────────────────────────────────────>
      │                      │                      │
      │ 4. Callback with     │                      │
      │    auth code         │                      │
      │<─────────────────────────────────────────────
      │                      │                      │
      │ 5. Exchange code     │                      │
      │─────────────────────>│                      │
      │                      │                      │
      │                      │ 6. Fetch service     │
      │                      │    tokens            │
      │                      │─────────────────────>│
      │                      │                      │
      │                      │<─────────────────────│
      │                      │                      │
      │ 7. Return gimme      │                      │
      │    session token     │                      │
      │<─────────────────────│                      │
      │                      │                      │
```

### Token Exchange

```bash
POST https://auth.gimme.tools/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
&code=auth_code_here
&redirect_uri=https://app.gimme.tools/callback
&client_id=gimme_client
&code_verifier=original_random_string
```

### Token Response

```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIs...",
  "token_type": "Bearer",
  "expires_in": 300,
  "refresh_token": "rt_abc123xyz",
  "scope": "GET:slack.gimme.tools/channels/*"
}
```

## JWT Token Structure

### Standard Claims

```json
{
  "iss": "auth.gimme.tools",
  "sub": "user_12345",
  "aud": "slack.gimme.tools",
  "exp": 1704067200,
  "iat": 1704066900,
  "nbf": 1704066900,
  "jti": "tok_unique_id"
}
```

### WebSpec-Specific Claims

```json
{
  "iss": "auth.gimme.tools",
  "sub": "user_12345",
  "aud": "slack.gimme.tools",
  "exp": 1704067200,
  "scope": "GET:channels/*,POST:channels/*/messages",
  "session_id": "sess_abc123",
  "device_id": "dev_xyz789",
  "platform_session": "plat_session_ref",
  "service_connection": "conn_slack_12345"
}
```

### Claim Definitions

| Claim | Type | Description |
|-------|------|-------------|
| `iss` | string | Token issuer (always auth.gimme.tools) |
| `sub` | string | User identifier |
| `aud` | string | Target subdomain (audience) |
| `exp` | number | Expiration timestamp (Unix) |
| `scope` | string | Permission scopes (comma-separated) |
| `session_id` | string | Current session identifier |
| `device_id` | string | Bound device identifier |
| `platform_session` | string | Reference to platform session |
| `service_connection` | string | Reference to service connection |

## Token Validation Algorithm

```javascript
async function validateToken(token, request) {
  // 1. Decode and verify signature
  const payload = await verifyJWT(token, publicKey);
  if (!payload) {
    return { valid: false, error: 'Invalid signature' };
  }

  // 2. Check expiration
  const now = Math.floor(Date.now() / 1000);
  if (payload.exp < now) {
    return { valid: false, error: 'Token expired' };
  }

  // 3. Check not-before
  if (payload.nbf && payload.nbf > now) {
    return { valid: false, error: 'Token not yet valid' };
  }

  // 4. Verify audience matches request subdomain
  const requestHost = new URL(request.url).hostname;
  if (payload.aud !== requestHost) {
    return { valid: false, error: 'Audience mismatch' };
  }

  // 5. Verify scope covers request
  const requestScope = `${request.method}:${request.path}`;
  if (!scopeMatches(payload.scope, requestScope)) {
    return { valid: false, error: 'Insufficient scope' };
  }

  // 6. Verify device binding (for local.gimme.tools)
  if (requestHost === 'local.gimme.tools') {
    if (!await verifyDevice(payload.device_id)) {
      return { valid: false, error: 'Device not verified' };
    }
  }

  return { valid: true, payload };
}
```

## Keychain Integration

### Zero-Friction Discovery

WebSpec integrates with device keychains to enable passwordless connection:

```
┌────────────────────────────────────────────────────────────┐
│                    User's Device                            │
│                                                             │
│  ┌──────────────────┐    ┌──────────────────────────────┐  │
│  │ Password Manager │    │        gimme.tools           │  │
│  │  (1Password,     │    │                              │  │
│  │   Keychain, etc.)│    │  1. Query keychain for       │  │
│  │                  │    │     matching domains         │  │
│  │  - slack.com     │───>│                              │  │
│  │  - notion.so     │    │  2. Suggest connections      │  │
│  │  - github.com    │    │     without password entry   │  │
│  │                  │    │                              │  │
│  └──────────────────┘    │  3. Use biometric to         │  │
│                          │     authorize OAuth flow     │  │
│                          └──────────────────────────────┘  │
└────────────────────────────────────────────────────────────┘
```

### Keychain Query Flow

```javascript
async function discoverConnections(user) {
  // 1. Get user's keychain hints (domains they have credentials for)
  const keychainDomains = await queryKeychain(user.device_id);

  // 2. Match against registered services
  const matches = [];
  for (const domain of keychainDomains) {
    const service = await findServiceByDomain(domain);
    if (service) {
      matches.push({
        service: service.id,
        domain: domain,
        status: 'KEYCHAIN',
        connectAction: 'biometric'
      });
    }
  }

  return matches;
}
```

### Connection Status

| Status | Description | UX |
|--------|-------------|-----|
| CONNECTED | OAuth already authorized | Direct access |
| KEYCHAIN | Credentials in password manager | "Connect with FaceID" |
| AVAILABLE | Service in registry | "Sign up / Connect" |
| UNAVAILABLE | Not in registry | Cannot connect |

## Token Types & Lifetimes

| Token Type | Lifetime | Storage | Refresh |
|------------|----------|---------|---------|
| Platform session | 7 days | HttpOnly cookie | Yes |
| Service connection | 30 days | Secure storage | Yes |
| Invocation token | 5 minutes | Memory only | No |
| Refresh token | 90 days | Secure storage | Yes |

### Token Refresh Flow

```bash
POST https://auth.gimme.tools/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token
&refresh_token=rt_abc123xyz
&client_id=gimme_client
```

## Device Binding

### Device Registration

```json
{
  "device_id": "dev_xyz789",
  "device_name": "MacBook Pro",
  "registered_at": "2024-01-01T00:00:00Z",
  "last_used": "2024-01-15T10:30:00Z",
  "capabilities": ["local_bridge", "biometric"],
  "verified": true
}
```

### Device Verification

For sensitive operations (especially local.gimme.tools):

```javascript
async function verifyDevice(deviceId, challenge) {
  // 1. Generate challenge
  const challenge = generateSecureRandom();

  // 2. Sign with device key (Secure Enclave/TPM)
  const signature = await deviceSign(challenge);

  // 3. Verify signature
  return verifyDeviceSignature(deviceId, challenge, signature);
}
```

## Onboarding Flow

### Progressive Authorization

```
┌─────────────────────────────────────────────────────────────┐
│  Step 1: Platform Account                                    │
│  "Create your gimme.tools account"                           │
│  → Email/password or SSO                                     │
├─────────────────────────────────────────────────────────────┤
│  Step 2: Device Registration                                 │
│  "Register this device"                                      │
│  → Biometric enrollment                                      │
├─────────────────────────────────────────────────────────────┤
│  Step 3: Keychain Discovery                                  │
│  "We found services you might use"                           │
│  → Show KEYCHAIN matches                                     │
├─────────────────────────────────────────────────────────────┤
│  Step 4: First Connection                                    │
│  "Connect your first service"                                │
│  → OAuth flow with one-tap biometric                         │
└─────────────────────────────────────────────────────────────┘
```

## Validation Checklist

When reviewing authentication and authorization:

- [ ] OAuth 2.0 with PKCE is used (no implicit flow)
- [ ] JWT signature is verified with correct public key
- [ ] Token expiration is checked before use
- [ ] Audience claim matches target subdomain
- [ ] Scope is sufficient for requested action
- [ ] Device binding verified for local operations
- [ ] Refresh tokens stored securely
- [ ] Session tokens use HttpOnly cookies
- [ ] No tokens logged or exposed in URLs
- [ ] Token refresh happens before expiration

## Common Auth Issues

| Issue | Risk | Fix |
|-------|------|-----|
| Missing PKCE | Authorization code interception | Add code_challenge |
| Long-lived tokens | Token theft impact | Use short-lived + refresh |
| Scope too broad | Principle of least privilege | Request minimal scopes |
| Missing aud check | Token replay attacks | Validate audience |
| Tokens in URLs | Log exposure | Use Authorization header |
| No device binding | Unauthorized device access | Require device verification |
