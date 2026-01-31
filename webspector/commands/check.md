---
name: check
description: Quick check of a specific WebSpec standard area
arguments:
  - name: area
    description: "Area to check: url-grammar (url), http-methods (http), subdomain-architecture (subdomain), authentication-authorization (auth), service-discovery (discovery)"
    required: true
  - name: action
    description: "Action to take: comment (add inline), suggest (show suggestions), revise (auto-fix)"
    required: false
  - name: verbose
    description: Include full explanations with spec references
    required: false
    type: boolean
---

# WebSpec Area-Specific Check

Run a focused WebSpec compliance check on a specific standard area.

## Available Areas

| Area | Aliases | Focus |
|------|---------|-------|
| `url-grammar` | `url` | URL patterns, REST hierarchy, format suffixes |
| `http-methods` | `http` | Method semantics, permission scoping, tokenization |
| `subdomain-architecture` | `subdomain` | Isolation, cookie/token scoping, origins |
| `authentication-authorization` | `auth` | OAuth flows, JWT tokens, sessions |
| `service-discovery` | `discovery` | NLP resolution, embeddings, three-way join |

## Usage Examples

```bash
# Check URL grammar
/webspector:check url-grammar
/webspector:check url

# Check HTTP methods with verbose output
/webspector:check http-methods --verbose
/webspector:check http --verbose

# Check auth with suggestions
/webspector:check auth --action=suggest

# Check subdomain security with auto-fix
/webspector:check subdomain --action=revise

# Check discovery with inline comments
/webspector:check discovery --action=comment
```

## Area-Specific Checks

### URL Grammar (`url-grammar` / `url`)

Validates:
- Subdomain is valid provider identifier
- Path follows REST collection/id pattern
- Format suffixes are standard (.json, .md, .pdf, etc.)
- Query params are filtering only (not hierarchy)
- No provider redundancy in path

### HTTP Methods (`http-methods` / `http`)

Validates:
- Methods match semantic intent (GET for read, POST for create, etc.)
- Permission scopes follow `METHOD:host/path_pattern` grammar
- Payloads use METHOD tokenization for action verbs
- Idempotency requirements are met
- HEAD/OPTIONS used appropriately

### Subdomain Architecture (`subdomain-architecture` / `subdomain`)

Validates:
- Cookies scoped to specific subdomains (not root)
- HttpOnly, Secure, SameSite flags present
- JWT audience claims match subdomain
- CORS configuration is not overly permissive
- Local bridge uses device binding

### Authentication (`authentication-authorization` / `auth`)

Validates:
- OAuth 2.0 with PKCE implementation
- JWT signature verification
- Token expiration checks
- Audience and issuer validation
- Scope verification
- Refresh token handling

### Service Discovery (`service-discovery` / `discovery`)

Validates:
- Intent extraction handles common verbs/objects
- Verb normalization to HTTP method categories
- Multi-vector embedding schema
- Three-way join includes connected, keychain, available
- Scoring formula with proper weights

## Output Format

### Default (Concise)

```
WebSpec Check: HTTP Methods
═══════════════════════════════════════

Patterns checked: 8
Issues found: 2

⚠ warning: POST used for read operation
  src/api/messages.ts:45

⚠ warning: Raw verb in payload
  src/api/tasks.ts:112

Summary: 2 warnings, 0 critical
```

### With `--verbose`

```
WebSpec Check: HTTP Methods
═══════════════════════════════════════

Patterns checked: 8
Issues found: 2

⚠ warning: POST used for read operation
  Location: src/api/messages.ts:45
  Code: app.post('/api/messages', async (req, res) => { ... })

  WebSpec Requirement:
    HTTP methods carry semantic meaning. POST is for creating
    new resources, while GET should be used for reading.

  Reference: HTTP Methods > Method Semantics

  Suggestion:
    Change to: app.get('/api/messages', async (req, res) => { ... })

...
```

### With `--action=suggest`

Provides code suggestions without modifying files.

### With `--action=comment`

Adds inline comments at issue locations.

### With `--action=revise`

Proposes fixes with confirmation before applying.

## Skills Used

Each area check uses the corresponding skill:
- `url-grammar` area → url-grammar skill
- `http-methods` area → http-methods skill
- `subdomain-architecture` area → subdomain-architecture skill
- `authentication-authorization` area → authentication-authorization skill
- `service-discovery` area → service-discovery skill
