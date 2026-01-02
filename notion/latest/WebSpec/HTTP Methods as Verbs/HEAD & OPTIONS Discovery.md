# HEAD & OPTIONS Discovery

Two HTTP methods enable powerful discovery patterns without custom APIs.

## HEAD - Lightweight Probing

`HEAD` is identical to `GET` but returns only headers, no body. Perfect for:

- Checking existence
- Getting metadata
- Validating permissions
- Cache validation

### Example: Check File Existence

```bash
HEAD [https://gdrive.gimme.tools/file/abc123](https://gdrive.gimme.tools/file/abc123)
```

Response:

```
HTTP/1.1 200 OK
Content-Type: application/pdf
Content-Length: 2048576
Last-Modified: Sat, 14 Dec 2024 10:30:00 GMT
ETag: "abc123-v2"
X-Gimme-Object: document.pdf
X-Gimme-Provider: gdrive
```

- `200` = exists, you have access
- `404` = doesn't exist
- `403` = exists, but no access
- No body transferred (bandwidth efficient)

### Example: Pre-flight Permission Check

```bash
# Can I delete this before trying?
HEAD [https://linear.gimme.tools/task/LIN-42](https://linear.gimme.tools/task/LIN-42)
```

Response headers indicate available operations:

```
X-Gimme-Allowed: GET, PATCH, DELETE
X-Gimme-Denied: PUT
```

### Metadata Without Fetch

```bash
# How big is this file before I download it?
HEAD [https://gdrive.gimme.tools/file/huge-video](https://gdrive.gimme.tools/file/huge-video)
```

```
Content-Length: 5368709120  # 5GB - maybe don't download on mobile
Content-Type: video/mp4
X-Gimme-Duration: 3600       # 1 hour video
```

---

## OPTIONS - Capability Discovery

`OPTIONS` asks "what can I do here?" Standard HTTP, but underutilized.

### Example: Service Capabilities

```bash
OPTIONS [https://slack.gimme.tools/](https://slack.gimme.tools/)
```

Response:

```
HTTP/1.1 200 OK
Allow: GET, POST, DELETE, HEAD, OPTIONS

{
  "service": "slack",
  "objects": {
    "message": {
      "methods": ["GET", "POST", "DELETE"],
      "types": ["text", "html", "blocks"]
    },
    "file": {
      "methods": ["GET", "POST", "DELETE"],
      "types": ["*"]
    },
    "channel": {
      "methods": ["GET"],
      "types": []
    }
  }
}
```

### Example: Object-Specific Discovery

```bash
OPTIONS [https://slack.gimme.tools/message](https://slack.gimme.tools/message)
```

Response:

```
Allow: GET, POST, DELETE
X-Gimme-Scopes-Required:
  GET: "GET:/message.*"
  POST: "POST:/message.*"
  DELETE: "DELETE:/message.*"

{
  "object": "message",
  "methods": {
    "GET": {
      "description": "Retrieve messages",
      "params": {
        "channel": {"type": "string", "required": false},
        "limit": {"type": "integer", "default": 50},
        "q": {"type": "string", "description": "Search query"}
      }
    },
    "POST": {
      "description": "Send a message",
      "params": {
        "channel": {"type": "string", "required": true},
        "body": {"type": "string", "required": true},
        "thread": {"type": "string", "required": false}
      }
    },
    "DELETE": {
      "description": "Delete a message",
      "requires_id": true
    }
  }
}
```

### Example: Resolution Preview

```bash
OPTIONS [https://api.gimme.tools/resolve?path=/message&body=hello](https://api.gimme.tools/resolve?path=/message&body=hello)
```

```
X-Gimme-Resolved: /message.text.slack
X-Gimme-Alternatives: /[message.text.email](http://message.text.email), /message.text.sms
X-Gimme-Confidence: 0.85
```

Clients can preview resolution before committing.

---

## CORS Pre-flight (Bonus)

Browsers already send `OPTIONS` for CORS pre-flight. WebSpec piggybacks:

```
# Browser sends automatically:
OPTIONS /message.slack
Access-Control-Request-Method: POST
Access-Control-Request-Headers: Authorization

# Server responds with CORS + WebSpec info:
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, DELETE
Access-Control-Allow-Headers: Authorization
X-Gimme-Scopes-Required: POST:/message.slack
```

One request, two purposes.

---

## Discovery Flow

```
Client: "What can I do?"
        │
        ▼
 OPTIONS [https://slack.gimme.tools/](https://slack.gimme.tools/)
        │
        ▼
┌────────────────────────────┐
│ Available objects:          │
│  - message (GET/POST/DELETE)│
│  - file (GET/POST/DELETE)   │
│  - channel (GET)            │
└────────────────────────────┘
        │

```

        ▼
Client: "Tell me about messages"
        │
        ▼
 OPTIONS [https://slack.gimme.tools/message](https://slack.gimme.tools/message)
        │
        ▼
┌────────────────────────────┐
│ GET params: channel, limit  │
│ POST params: channel, body  │
│ DELETE requires: id         │
└────────────────────────────┘
        │
        ▼
 Client now knows how to use the API.
 No documentation needed.

---

## Self-Documenting APIs

Every WebSpec endpoint is self-documenting via `OPTIONS`. This means:

- No separate API documentation to maintain
- Clients can discover capabilities at runtime
- Schema changes are immediately visible
- AI agents can learn APIs dynamically

`[TODO:IMAGE] Diagram showing OPTIONS request/response flow with capability discovery`