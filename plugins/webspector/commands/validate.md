---
name: validate
description: Run a full WebSpec compliance check on the current codebase
arguments:
  - name: action
    description: "Action to take: comment (add inline), suggest (show suggestions), revise (auto-fix)"
    required: false
  - name: verbose
    description: Include full explanations with spec references
    required: false
    type: boolean
---

# WebSpec Full Compliance Validation

Run a comprehensive WebSpec compliance check across all standard areas.

## Validation Areas

1. **URL Grammar** - REST URL patterns, format suffixes, path hierarchy
2. **HTTP Methods** - Method semantics, permission scoping, tokenization
3. **Subdomain Architecture** - Isolation model, cookie/token scoping
4. **Authentication & Authorization** - OAuth flows, JWT tokens, sessions
5. **Service Discovery** - NLP resolution, embedding schema, three-way join

## Process

1. Scan the codebase for relevant code patterns
2. Validate each pattern against WebSpec standards
3. Report issues with severity and location
4. Provide fixes based on `--action` flag

## Output Modes

### Default (Concise)

```
WebSpec Compliance Report
═══════════════════════════════════════

✓ URL Grammar: 12 patterns checked, 0 issues
⚠ HTTP Methods: 8 patterns checked, 2 issues
✓ Subdomain Architecture: 5 patterns checked, 0 issues
✗ Authentication: 6 patterns checked, 1 critical issue
✓ Service Discovery: 3 patterns checked, 0 issues

Issues Found:
  [HTTP-METHOD] warning: POST used for read operation
    src/api/messages.ts:45
  [HTTP-METHOD] warning: Raw verb in payload
    src/api/tasks.ts:112
  [AUTH-FLOW] critical: Missing PKCE in OAuth flow
    src/auth/oauth.ts:28

Total: 3 issues (1 critical, 2 warnings)
```

### With `--verbose`

Includes spec references and detailed explanations for each issue.

### With `--action=suggest`

Provides specific code suggestions for each issue without modifying files.

### With `--action=comment`

Adds inline comments to code at issue locations:

```javascript
// [WEBSPEC] warning: POST used for read operation
// Use GET for read semantics (see HTTP Methods skill)
app.post('/messages', ...)
```

### With `--action=revise`

Proposes automatic fixes with confirmation:

```
Proposed fixes:
  1. src/api/messages.ts:45 - Change POST to GET
  2. src/api/tasks.ts:112 - Tokenize verb as [M:DELETE]
  3. src/auth/oauth.ts:28 - Add PKCE code_challenge

Apply these fixes? [y/N]
```

## Usage Examples

```bash
/webspector:validate                     # Concise summary
/webspector:validate --verbose           # Detailed report
/webspector:validate --action=suggest    # Show fix suggestions
/webspector:validate --action=comment    # Add inline comments
/webspector:validate --action=revise     # Auto-fix with confirmation
```

## Skills Used

This command leverages all Webspector skills:
- url-grammar
- http-methods
- subdomain-architecture
- authentication-authorization
- service-discovery
