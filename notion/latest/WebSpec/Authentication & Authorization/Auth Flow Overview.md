# Auth Flow Overview

WebSpec authentication uses standard OAuth 2.0 with platform-level authorization and session-scoped tokens.

## Authorization Layers

| Layer | What's Authorized | Revocation Granularity |
| --- | --- | --- |
| **Platform** | [gimme.tools](http://gimme.tools) (OAuth client) | User revokes all sessions |
| **Session** | session-id + device-id | User revokes one session |
| **Invocation** | Per-request confirmation | Real-time control |

---

## Service Connection Flow

### Initial OAuth

When a user first connects a service:

```
1. User clicks "Connect Slack"
           │
           ▼
2. Redirect to Slack OAuth:
   [https://slack.com/oauth/authorize](https://slack.com/oauth/authorize)
     ?client_id=gimme-tools
     &redirect_uri=[https://auth.gimme.tools/callback](https://auth.gimme.tools/callback)
     &scope=chat:write,files:read
     &state={session_id}:{nonce}
           │
           ▼
3. User authorizes on Slack
           │
           ▼
4. Redirect back:
   [https://auth.gimme.tools/callback](https://auth.gimme.tools/callback)
     ?code=xxx
     &state={session_id}:{nonce}
           │
           ▼
5. [auth.gimme.tools](http://auth.gimme.tools) exchanges code for tokens:
   - access_token (for Slack API)
   - refresh_token (for renewal)
   Stored encrypted, associated with user
           │
           ▼
6. User can now use Slack via [gimme.tools](http://gimme.tools)
```

### Key Points

- OAuth client is always [`gimme.tools`](http://gimme.tools) (not per-session)
- Services see [gimme.tools](http://gimme.tools) as a standard OAuth client
- [gimme.tools](http://gimme.tools) manages sub-delegation internally
- Users revoke at service level via [gimme.tools](http://gimme.tools) UI
- Services can revoke [gimme.tools](http://gimme.tools) if it misbehaves

---

## Session Authentication

### Login Flow

```
1. User visits [gimme.tools](http://gimme.tools)
           │
           ▼
2. Login via:
   - Email + password
   - Social login (Google, GitHub, etc.)
   - Passkey/WebAuthn
           │
           ▼
3. [auth.gimme.tools](http://auth.gimme.tools) sets session cookie:
   Set-Cookie: session=xxx;
     Domain=[auth.gimme.tools](http://auth.gimme.tools);
     HttpOnly; Secure; SameSite=Strict
           │
           ▼
4. Session established. User can request service tokens.
```

### Service Token Request

```
1. Client needs to call [slack.gimme.tools](http://slack.gimme.tools)
           │
           ▼
2. Request service token:
   POST [https://auth.gimme.tools/token](https://auth.gimme.tools/token)
   Cookie: session=xxx
   Body: {
     "service": "[slack.gimme.tools](http://slack.gimme.tools)",
     "scope": ["GET:/message.*", "POST:/message.*"]
   }
           │
           ▼
3. [auth.gimme.tools](http://auth.gimme.tools) validates:
   - Session cookie valid?
   - User has connected Slack?
   - Requested scope ⊆ user's authorized scope?
           │
           ▼
4. Returns scoped token:
   {
     "token": "eyJ...",
     "expires_in": 3600,
     "scope": ["GET:/message.*", "POST:/message.*"]
   }
           │
           ▼
5. Client uses token:
   GET [https://slack.gimme.tools/message](https://slack.gimme.tools/message)
   Authorization: Bearer eyJ...
```

---

## Complete Request Flow

```
┌────────┐   ┌────────────────────┐   ┌──────────────────┐   ┌──────────┐
│ Client │   │ [auth.gimme.tools](http://auth.gimme.tools) │   │[slack.gimme.tools](http://slack.gimme.tools) │   │ Slack API│
└────┬───┘   └────────┬───────────┘   └────────┬─────────┘   └────┬─────┘
     │                │                       │                  │
     │  1. Get token   │                       │                  │
     │───────────────▶│                       │                  │
     │                │                       │                  │
     │  2. Token       │                       │                  │
     │◀───────────────│                       │                  │
     │                │                       │                  │
     │  3. API call with Bearer token         │                  │
     │───────────────────────────────────────▶│                  │
     │                │                       │                  │
     │                │    4. Validate token   │                  │
     │                │◀───────────────────────│                  │
     │                │                       │                  │
     │                │                       │  5. Call Slack   │
     │                │                       │─────────────────▶│
     │                │                       │                  │
     │                │                       │  6. Response     │
     │                │                       │◀─────────────────│
     │                │                       │                  │
     │  7. Response                           │                  │
     │◀───────────────────────────────────────│                  │
```

---

## Token Refresh

Service tokens are short-lived (1 hour typical). Refresh is automatic:

```
1. Client's token approaching expiry
           │
           ▼
2. Request new token:
   POST [https://auth.gimme.tools/token/refresh](https://auth.gimme.tools/token/refresh)
   Cookie: session=xxx
   Body: {"service": "[slack.gimme.tools](http://slack.gimme.tools)"}
           │
           ▼
3. [auth.gimme.tools](http://auth.gimme.tools) validates session, issues new token
           │
           ▼
4. Client continues with new token
```

If the underlying OAuth token (from Slack) needs refresh, [auth.gimme.tools](http://auth.gimme.tools) handles it transparently.

---

## Revocation

### User Revokes Session

```bash
POST [https://auth.gimme.tools/sessions/{session_id}/revoke](https://auth.gimme.tools/sessions/{session_id}/revoke)
```

All tokens issued for that session become invalid.

### User Disconnects Service

```bash
POST [https://auth.gimme.tools/services/slack/disconnect](https://auth.gimme.tools/services/slack/disconnect)
```

- Revokes OAuth tokens at Slack
- Invalidates all [gimme.tools](http://gimme.tools) tokens for Slack
- User must re-authorize to reconnect

### Service Revokes [gimme.tools](http://gimme.tools)

If Slack revokes [gimme.tools](http://gimme.tools)'s OAuth access:

- All users' Slack integrations stop working
- Users see "Slack disconnected" error
- Users must re-authorize

---

## Security Checklist

- [ ]  Session cookies: HttpOnly, Secure, SameSite=Strict, auth subdomain only
- [ ]  Service tokens: Short-lived, audience-bound, scope-limited
- [ ]  OAuth tokens: Encrypted at rest, never exposed to clients
- [ ]  PKCE: Used for all OAuth flows
- [ ]  State parameter: Includes session ID + nonce, validated on callback
- [ ]  Token rotation: Refresh tokens rotated on use