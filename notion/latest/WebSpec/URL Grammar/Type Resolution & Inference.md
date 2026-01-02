# Type Resolution & Inference

When parts of the URL are omitted, WebSpec applies a resolution chain to infer the missing components.

## Resolution Priority

```
1. EXPLICIT      → use exactly what's specified
2. TYPE_HINT     → infer provider from type (.pdf → document providers)
3. PROVIDER_HINT → infer object from provider (.slack → message)
4. PAYLOAD       → analyze content to infer object + type
5. USER_DEFAULT  → fall back to user preferences
6. PROMPT        → ask user to disambiguate
```

## Examples by Specificity

| Request | Resolution | Notes |
| --- | --- | --- |
| `POST /message.text.slack` | Fully explicit | No inference needed |
| `POST /message.slack` | Type inferred | Default to .text |
| `POST /message` | Provider prompted | "Via Slack, Email, or SMS?" |
| `POST /.slack` | Object inferred | Slack's default = message |
| `POST /` with file body | Both inferred | Analyze payload MIME type |

---

## Inference Rules

### Type → Provider

Certain types strongly suggest providers:

```yaml
type_hints:
  .pdf:    [gdrive, dropbox, local]   # document providers
  .xlsx:   [gsheets, gdrive, local]   # spreadsheet providers
  .mp3:    [gdrive, spotify, local]   # audio providers
  .slack:  [slack]                    # provider IS type
  .linear: [linear]                   # provider IS type
```

### Provider → Object

Providers have default object types:

```yaml
provider_defaults:
  slack:   message     # Slack primarily handles messages
  gdrive:  file        # Drive primarily handles files
  linear:  task        # Linear primarily handles tasks
  gcal:    event       # Calendar handles events
  notion:  document    # Notion handles documents (or pages)
```

### Payload → Object + Type

Content-Type header or file analysis:

```yaml
payload_inference:
  "application/pdf":     document.pdf
  "image/png":           image.png
  "text/plain":          document.text
  "application/json":    data.json
  "text/csv":            data.csv
```

---

## User Defaults

Users can configure preferred providers per object type:

```yaml
user_preferences:
  message:     slack      # Default messaging to Slack
  file:        gdrive     # Default file storage to Drive
  task:        linear     # Default tasks to Linear
  document:    notion     # Default docs to Notion
```

When inference reaches step 5, these preferences are applied.

---

## Disambiguation Prompts

When resolution cannot determine a unique target, the user is prompted:

```
┌───────────────────────────────────────────────────────┐
│  Request: POST /message                           │
│  Body: "Hello team!"                              │
├───────────────────────────────────────────────────────┤
│                                                   │
│  Send via:                                        │
│                                                   │
│  ⭐ Slack (default)              [Send]           │
│     /message.slack                                │
│                                                   │
│  ✉️  Email                        [Send]           │
│     /[message.email](http://message.email)                                │
│                                                   │
│  📱 SMS                          [Send]           │
│     /message.sms                                  │
│                                                   │
└───────────────────────────────────────────────────────┘
```

The default (starred) comes from user preferences.

---

## Resolution API

Clients can preview resolution without executing:

```bash
# What would this resolve to?
OPTIONS /message?body=hello

# Response:
X-Gimme-Resolved: /message.text.slack
X-Gimme-Alternatives: /[message.text.email](http://message.text.email), /message.text.sms
X-Gimme-Confidence: 0.85
```

This enables UI to show resolution previews before confirmation.