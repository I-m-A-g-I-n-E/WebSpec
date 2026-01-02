# Registration Schema

How service providers register tools with the [gimme.tools](http://gimme.tools) ecosystem.

## Overview

Service registration is a two-phase process:

1. **Domain Verification** - Prove you control the service domain
2. **Tool Registration** - Define your tools and their semantics

---

## Service Manifest

Services provide a manifest file at a well-known location:

```
[https://your-service.com/.well-known/gimme-tools.yaml](https://your-service.com/.well-known/gimme-tools.yaml)
```

### Full Manifest Schema

```yaml
# Service metadata
service:
  name: "Acme Messaging"
  id: "acme"                          # Becomes [acme.gimme.tools](http://acme.gimme.tools)
  description: "Team messaging platform"
  icon: "[https://acme.com/icon.png](https://acme.com/icon.png)"
  homepage: "[https://acme.com](https://acme.com)"
  
  # Domains this service operates under
  canonical_domains:
    - [acme.com](http://acme.com)
    - "*.[acme.com](http://acme.com)"
    - [app.acme.io](http://app.acme.io)
  
  # OAuth configuration
  oauth:
    authorization_url: "[https://acme.com/oauth/authorize](https://acme.com/oauth/authorize)"
    token_url: "[https://acme.com/oauth/token](https://acme.com/oauth/token)"
    scopes:
      - name: "messages:read"
        description: "Read messages"
      - name: "messages:write"
        description: "Send messages"
      - name: "files:read"
        description: "Access files"

# Tool definitions
tools:
  - id: "send-message"
    route:
      method: POST
      path: /messages
      types: [text, html, blocks]
    
    semantics:
      canonical: "send a message via Acme"
      predicates: [send, post, message, notify, ping]
      objects: [message, note, notification]
    
    params:
      channel:
        type: string
        required: true
        description: "Channel or user to message"
      body:
        type: string
        required: true
        description: "Message content"
    
    oauth_scopes: ["messages:write"]

  - id: "read-messages"
    route:
      method: GET
      path: /messages
      types: [text, html]
    
    semantics:
      canonical: "read messages from Acme"
      predicates: [read, get, fetch, check]
      objects: [message, messages, inbox]
    
    params:
      channel:
        type: string
        required: false
      limit:
        type: integer
        default: 50
      since:
        type: datetime
        required: false
    
    oauth_scopes: ["messages:read"]

  - id: "delete-message"
    route:
      method: DELETE
      path: /messages/{id}
    
    semantics:
      canonical: "delete a message"
      predicates: [delete, remove, trash]
      objects: [message]
    
    oauth_scopes: ["messages:write"]
```

---

## Registration API

### Submit Registration

```bash
POST [https://api.gimme.tools/services/register](https://api.gimme.tools/services/register)
Authorization: Bearer {admin_token}
Content-Type: application/json

{
  "manifest_url": "[https://acme.com/.well-known/gimme-tools.yaml](https://acme.com/.well-known/gimme-tools.yaml)",
  "contact_email": "[integrations@acme.com](mailto:integrations@acme.com)"
}
```

### Response

```json
{
  "status": "pending_verification",
  "service_id": "acme",
  "verification": {
    "method": "dns",
    "record": "_[gimme-verify.acme.com](http://gimme-verify.acme.com)",
    "value": "gimme-tools-verify=abc123"
  },
  "next_steps": [
    "Add DNS TXT record for verification",
    "Call /services/{service_id}/verify when ready"
  ]
}
```

### Verify Domain

```bash
POST https://api.gimme.tools/services/{service_id}/verify
Authorization: Bearer {admin_token}

# Example:
POST https://api.gimme.tools/services/acme/verify
```

### Successful Verification

```json
{
  "status": "active",
  "service_id": "acme",
  "subdomain": "[acme.gimme.tools](http://acme.gimme.tools)",
  "tools_registered": 3,
  "embeddings_generated": true
}
```

---

## Manifest Validation

Registration validates:

| Check | Description |
| --- | --- |
| Schema validity | YAML matches expected schema |
| URL accessibility | Manifest URL returns 200 |
| OAuth endpoints | Authorization and token URLs respond |
| Unique ID | Service ID not already taken |
| Domain ownership | Canonical domains verified |

---

## Tool Schema Details

### Route Definition

```yaml
route:
  method: POST | GET | PUT | PATCH | DELETE | HEAD | OPTIONS
  path: /object[.type][/{param}]
  types: [type1, type2]  # Supported type suffixes
```

### Semantics Definition

```yaml
semantics:
  # Required: Primary description for embedding
  canonical: "human-readable description of what this does"
  
  # Required: Verbs that map to this tool
  predicates:
    - primary_verb
    - synonym1
    - synonym2
  
  # Required: Nouns that this tool operates on
  objects:
    - primary_object
    - synonym1
  
  # Optional: Context hints for disambiguation
  contexts:
    - "workplace"
    - "team communication"

  # Optional: Semantic attributes for ranking
  urgency: [low, medium, high]
  formality: [casual, formal]
  
  # Optional: What this tool is NOT (negative examples)
  not:
    - "send email"  # Different tool
    - "schedule message"  # Different capability
```

### Parameter Definition

```yaml
params:
  param_name:
    type: string | integer | boolean | datetime | file
    required: true | false
    default: <value>  # Optional default
    description: "Human readable description"
    enum: [value1, value2]  # Optional allowed values
    pattern: "regex"  # Optional validation pattern
```

---

## Update Flow

To update tools after initial registration:

```bash
POST [https://api.gimme.tools/services/{service_id}/sync](https://api.gimme.tools/services/{service_id}/sync)
Authorization: Bearer {admin_token}
```

This re-fetches the manifest and:

- Adds new tools
- Updates changed tools
- Removes deleted tools
- Regenerates embeddings

---

## Webhook Notifications

Services can register webhooks for events:

```yaml
webhooks:
  user_connected:
    url: "[https://acme.com/webhooks/gimme/connected](https://acme.com/webhooks/gimme/connected)"
    secret: "whsec_xxx"
    
  user_disconnected:
    url: "[https://acme.com/webhooks/gimme/disconnected](https://acme.com/webhooks/gimme/disconnected)"
    secret: "whsec_xxx"
    
  tool_invoked:
    url: "[https://acme.com/webhooks/gimme/invoked](https://acme.com/webhooks/gimme/invoked)"
    secret: "whsec_xxx"
```

---

## Rate Limits

| Operation | Limit |
| --- | --- |
| Registration attempts | 10/hour |
| Manifest syncs | 100/day |
| API calls per service | 10,000/hour |
| Webhook deliveries | 1,000/minute |