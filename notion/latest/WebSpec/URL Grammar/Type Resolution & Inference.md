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

| User Intent / Input | Resolved Canonical URL | Notes |
| --- | --- | --- |
| `POST slack.gimme.tools/messages` | `POST slack.gimme.tools/messages` | Fully explicit |
| `POST /messages.slack` | `POST slack.gimme.tools/messages` | Provider suffix interpreted as resolution hint |
| `POST /messages` | (Prompt: "Via Slack, Email, or SMS?") | Provider prompted |
| `POST /.slack` | `POST slack.gimme.tools/messages` | Object inferred (Slack default = messages) |
| `POST /` with file body | (Varies by MIME type) | Analyze payload MIME type |

---

## Inference Rules

### Type → Provider

Certain extensions or pseudo-suffixes strongly suggest providers:

```yaml
type_hints:
  .pdf:    [gdrive, dropbox, local]   # document providers (valid format suffix)
  .xlsx:   [gsheets, gdrive, local]   # spreadsheet providers (valid format suffix)
  .mp3:    [gdrive, spotify, local]   # audio providers (valid format suffix)
  .slack:  [slack]                    # provider hint (pseudo-suffix)
  .linear: [linear]                   # provider hint (pseudo-suffix)
```

### Provider → Object

Providers have default object types:

```yaml
provider_defaults:
  slack:   messages    # Slack primarily handles messages
  gdrive:  files       # Drive primarily handles files
  linear:  issues      # Linear primarily handles issues
  gcal:    events      # Calendar handles events
  notion:  pages       # Notion handles pages
```

### Payload → Object + Type

Content-Type header or file analysis:

```yaml
payload_inference:
  "application/pdf":     documents.pdf
  "image/png":           images.png
  "text/plain":          documents.text
  "application/json":    data.json
  "text/csv":            data.csv
```

---

## User Defaults

Users can configure preferred providers per object type:

```yaml
user_preferences:
  messages:    slack      # Default messaging to Slack
  files:       gdrive     # Default file storage to Drive
  issues:      linear     # Default tasks to Linear
  pages:       notion     # Default docs to Notion
```

When inference reaches step 5, these preferences are applied.

---

## Disambiguation Prompts

When resolution cannot determine a unique target, the user is prompted:

```
┌───────────────────────────────────────────────────────┐
│  Request: POST /messages                          │
│  Body: "Hello team!"                              │
├───────────────────────────────────────────────────────┤
│                                                   │
│  Send via:                                        │
│                                                   │
│  ⭐ Slack (default)              [Send]           │
│     slack.gimme.tools/messages                    │
│                                                   │
│  ✉️  Email                        [Send]           │
│     gmail.google.gimme.tools/messages             │
│                                                   │
│  📱 SMS                          [Send]           │
│     sms.twilio.gimme.tools/messages               │
│                                                   │
└───────────────────────────────────────────────────────┘
```

The default (starred) comes from user preferences.

---

## Resolution API

Clients can preview resolution without executing:

```bash
# What would this resolve to?
OPTIONS /resolve?path=/messages&body=hello

# Response:
X-Gimme-Resolved: https://slack.gimme.tools/messages
X-Gimme-Alternatives: https://gmail.google.gimme.tools/messages, https://sms.twilio.gimme.tools/messages
X-Gimme-Confidence: 0.85
```

This enables UI to show resolution previews before confirmation.