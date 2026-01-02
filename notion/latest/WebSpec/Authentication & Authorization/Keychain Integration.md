# Keychain Integration

Password managers already know which services you use. WebSpec leverages this for **zero-friction service discovery**.

## The Insight

Traditional tool platforms ask: *"What services do you want to connect?"*

WebSpec flips it: *"What services do you already use?"* — and your password manager already knows.

---

## The Three-Way Join

---

## Match Priority

```
┌───────────────────────────────────────────────────────────────┐
│                      User's Request                          │
│            "send this report to the team Slack"               │
│                           │                                   │
│              NLP extracts embeddings                          │
│         action: [send] ──▶ 🔵 action_embedding                │
│         object: [message, slack] ──▶ 🟢 object_embedding        │
└───────────────────────────────────────────────────────────────┘
                            │
           ┌────────────────┼────────────────┐
           ▼                ▼                ▼
    ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
    │  CONNECTED   │  │  AVAILABLE   │  │   KEYCHAIN   │
    │  SERVICES    │  │  SERVICES    │  │   SERVICES   │
    │             │  │             │  │              │
    │ (authorized)│  │ (registry)  │  │ (1Password,  │
    │             │  │             │  │  Keychain)   │
    └─────────────┘  └─────────────┘  └─────────────┘
           │                │                │
           └────────────────┼────────────────┘
                            ▼
                    ┌───────────────┐
                    │  MATCH MATRIX │
                    └───────────────┘
```

| Match Type | UX | Action |
| --- | --- | --- |
| **Connected + Match** | ✅ Slack (connected) | [Use This] |
| **Keychain + Available** | 🔐 Slack (in 1Password) | [Connect - FaceID] |
| **Available only** | 🔗 Slack (available) | [Sign Up / Connect] |
| **No match** | ❓ | "No services found for this" |

---

## Supported Keychain Providers

| Provider | API | Match Fields |
| --- | --- | --- |
| **1Password** | Connect API | url, domain, tags |
| **Apple Keychain** | AuthenticationServices | domain, associated_domains |
| **Google Passwords** | (if available) | domain, app_id |
| **Bitwarden** | CLI / API | uri, name |

---

## Domain Matching Logic

```yaml
# User's keychain entry
keychain_entry:
  domain: [slack.com](http://slack.com)
  username: [preston@imajn.ai](mailto:preston@imajn.ai)
  
# [gimme.tools](http://gimme.tools) service registry
service:
  id: [slack.gimme.tools](http://slack.gimme.tools)
  canonical_domains:
    - [slack.com](http://slack.com)
    - "*.[slack.com](http://slack.com)"
    - [app.slack.com](http://app.slack.com)
    
# Match algorithm
match: [slack.com](http://slack.com) ∈ canonical_domains ✓
```

---

## Biometric Auth Flow

```
User: "post this to Slack"
              │
              ▼
┌─────────────────────────────────────┐
│ [gimme.tools](http://gimme.tools):                       │
│ "Found Slack in your keychain.     │
│  Connect with Face ID?"            │
│                                     │
│     [🔐 Connect with Face ID]       │
└─────────────────────────────────────┘
              │
              ▼ (user taps)
┌─────────────────────────────────────┐
│ 1Password / Keychain prompt        │
│ [Face ID / Touch ID scan]          │
└─────────────────────────────────────┘
              │
              ▼ (success)
┌─────────────────────────────────────┐
│ [gimme.tools](http://gimme.tools) receives:              │
│   - domain: [slack.com](http://slack.com)              │
│   - credential_id (NOT password)   │
│   - proof of biometric auth        │
└─────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│ OAuth flow with login_hint:        │
│ [slack.com/oauth?login_hint=](http://slack.com/oauth?login_hint=)...     │
│ (often skips login, just consent)  │
└─────────────────────────────────────┘
              │
              ▼
User connected. Original action proceeds.
```

---

## What We're NOT Doing

<aside>
⚠️

**Critical**: [gimme.tools](http://gimme.tools) never receives or stores passwords.

</aside>

The keychain integration is for **discovery only**:

1. Keychain reveals which **domains** user has credentials for
2. Biometric proves **user identity**
3. OAuth **authorizes [gimme.tools](http://gimme.tools)** separately
4. Keychain accelerates discovery + may autofill OAuth login

---

## User Data Model

```yaml
user:
  id: user-123
  
  connected_services:              # OAuth tokens stored
    - service: [slack.gimme.tools](http://slack.gimme.tools)
      token: {encrypted}
      scopes: ["POST:slack.gimme.tools/messages/*", "GET:slack.gimme.tools/files/*"]
      
  keychain_hints:                  # Discovered, not yet connected
    - domain: [notion.so](http://notion.so)
      keychain: 1password
      available_service: [notion.gimme.tools](http://notion.gimme.tools)  # Matched!
      
    - domain: [linear.app](http://linear.app)
      keychain: 1password  
      available_service: [linear.gimme.tools](http://linear.gimme.tools)
      
    - domain: [obscure-saas.io](http://obscure-saas.io)
      keychain: 1password
      available_service: null  # No integration yet
```

---

## Prompt UX

```
┌──────────────────────────────────────────────────────────┐
│ 🔍 "send the quarterly report to the design channel"   │
├──────────────────────────────────────────────────────────┤
│                                                          │
│ ✅ Slack (connected)                        [Use This]    │
│    POST slack.gimme.tools/files                          │
│                                                          │
│ 🔐 Discord (in 1Password)               [Connect - FaceID]│
│    POST discord.gimme.tools/files                        │
│                                                          │
│ 🔗 Microsoft Teams (available)          [Sign Up / Connect]│
│    POST teams.gimme.tools/files                          │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

---

## Integration Requests

When keychain has entries with no [gimme.tools](http://gimme.tools) match:

```
┌────────────────────────────────────────────┐
│ We noticed you use [obscure-saas.io](http://obscure-saas.io).        │
│ Want to request an integration?            │
│                                            │
│              [👍 Upvote]                    │
└────────────────────────────────────────────┘
```

Crowdsourced integration prioritization, driven by actual user credential data.