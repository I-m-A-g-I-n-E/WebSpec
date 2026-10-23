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

Run a WebSpec compliance check across all areas. Every finding cites the rule ID it rests on,
from the spec at https://i-m-a-g-i-n-e.github.io/WebSpec/spec/.

## Validation Areas

1. **URL Grammar** - destination hosts, the route rule and route map, arguments (HG, PA, AR)
2. **HTTP Methods** - tool contracts, method binding, `WebSpec-Definer`, responses (TC, MB,
   DF, RQ, RS)
3. **Subdomain Architecture** - destinations, qualifiers, public exposure, proxy, CORS (HG,
   DP-5, DP-8)
4. **Authentication & Authorization** - levels 1 to 4: guard, `Idempotency-Key`, clearance,
   approval, key custody (LV, GD, ID, CL, AP, CO, SH)
5. **Service Discovery** - `GET /`, `OPTIONS`, `HEAD`, requests built from the route map (DS,
   PA-2, SH-2)

## Process

1. Find the code that matters: MCP servers and their tool annotations, clients and shims that
   call a WebSpec gateway, gateway and proxy configuration
2. Check each against the rules of its area
3. Report issues with severity, location, and rule ID
4. Provide fixes based on the `--action` flag

Report only what the spec requires. Its [known limits](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/status/#known-limits)
are not implementation bugs, and its [roadmap](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/status/#roadmap)
items are not requirements.

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
  [HTTP-METHOD] warning: POST without WebSpec-Definer (DF-1)
    src/harness/send.ts:45
  [HTTP-METHOD] warning: harness reads X-UFO-Taint, which the gateway no longer sends (RS-2)
    src/harness/policy.ts:112
  [LEVELS] critical: guard key written to the agent's log (GD-5, SH-1)
    src/shim/config.ts:28

Total: 3 issues (1 critical, 2 warnings)
```

### With `--verbose`

Includes the rule text and a link to the spec for each issue.

### With `--action=suggest`

Provides specific code suggestions for each issue without modifying files.

### With `--action=comment`

Adds inline comments to code at issue locations:

```javascript
// [WEBSPEC] warning: POST without WebSpec-Definer (DF-1)
// Send the verb of the route map entry, here SEND
fetch(`${base}/email`, { method: 'POST', body })
```

### With `--action=revise`

Proposes automatic fixes with confirmation:

```
Proposed fixes:
  1. src/harness/send.ts:45 - Add header WebSpec-Definer: SEND
  2. src/harness/policy.ts:112 - Read WebSpec-Taint instead of X-UFO-Taint
  3. src/shim/config.ts:28 - Stop logging the guard key

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

This command uses all Webspector skills: `url-grammar`, `http-methods`,
`subdomain-architecture`, `authentication-authorization`, `service-discovery`.
