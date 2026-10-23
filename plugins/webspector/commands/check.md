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

Run a focused WebSpec compliance check on one area. Every finding cites the rule ID it rests
on, from the spec at https://i-m-a-g-i-n-e.github.io/WebSpec/spec/.

## Available Areas

| Area | Aliases | Rules | Focus |
|------|---------|-------|-------|
| `url-grammar` | `url` | HG, PA, AR | Destination hosts, the route rule and route map, single-segment paths, arguments |
| `http-methods` | `http` | TC, MB, DF, RQ, RS | Tool contracts and annotations, method binding, `WebSpec-Definer`, response headers |
| `subdomain-architecture` | `subdomain` | HG, DP-5, DP-8 | Destinations and qualifiers, public exposure, proxy forwarding, CORS |
| `authentication-authorization` | `auth` | LV, GD, ID, CL, AP, CO, SH | Levels 1 to 4: guard and nonce, `Idempotency-Key`, clearance, human approval, key custody |
| `service-discovery` | `discovery` | DS, PA-2, SH-2 | `GET /`, `OPTIONS`, `HEAD`, building requests from the route map |

## Usage Examples

```bash
/webspector:check url-grammar
/webspector:check http --verbose
/webspector:check auth --action=suggest
/webspector:check subdomain --action=revise
/webspector:check discovery --action=comment
```

## Area-Specific Checks

### URL Grammar (`url-grammar` / `url`)

- Hosts are `{destination}.{domain}` with lowercase labels; qualifiers only if allow-listed
- Paths are the single-segment noun of the tool's route map entry, never `/{tool_name}` by
  guesswork or `/{verb}/{noun}`
- Ids and other arguments in the query or a JSON body; `GET` has no body; query keys once
- MCP tool names and annotations give the intended entry under the route rule

### HTTP Methods (`http-methods` / `http`)

- MCP annotations match what each tool does; unannotated tools take the strict defaults
- The method is one the tool's contract admits, and the one its entry maps
- Every `POST` carries one `WebSpec-Definer` verb from `POST`'s family; definers on `PUT`,
  `PATCH`, `DELETE` are from their method's family or absent
- `WebSpec-*` header names; no `X-Gimme-*`, `X-WebSpec-*`, or `X-UFO-*`
- `WebSpec-Taint: open-world` bodies treated as untrusted data

### Subdomain Architecture (`subdomain-architecture` / `subdomain`)

- Destinations on a public domain run at level 1 or higher
- The proxy forwards only WebSpec hosts and preserves `Host`; loopback names only locally
- `WEBSPEC_CORS_ORIGINS` unset or limited to trusted origins
- Tokens and keys never reused across destinations

### Authentication (`authentication-authorization` / `auth`)

- A fresh nonce per request; the guard message signs the trimmed path, canonical query,
  definer, and idempotency key
- `Idempotency-Key` on unsafe calls to non-idempotent tools from level 2, reused only to
  retry
- Clearances bound to the tool of the route map entry, minted within 30 s
- `428` retries repeat the same method, path, arguments, and body with fresh credentials
- The guard key stays in the shim, out of the model's context, logs, and the repo

### Service Discovery (`service-discovery` / `discovery`)

- Requests built from `GET /` or `OPTIONS /` entries, with their `requirements`
- Closed entries (`error`) not retried as transient
- The map refreshed after refusals on a key; `HEAD` not used to test item existence

## Output Format

### Default (Concise)

```
WebSpec Check: HTTP Methods
═══════════════════════════════════════

Patterns checked: 8
Issues found: 2

⚠ warning: POST without WebSpec-Definer (DF-1)
  src/harness/send.ts:45

⚠ warning: tool marked read-only but writes a file (TC-1)
  server/tools.py:112

Summary: 2 warnings, 0 critical
```

### With `--verbose`

Adds, for each issue, the code, the rule text it violates with a link to the spec, and a
suggested fix.

### With `--action=suggest`

Provides code suggestions without modifying files.

### With `--action=comment`

Adds inline comments at issue locations.

### With `--action=revise`

Proposes fixes with confirmation before applying.

## Skills Used

Each area check uses the skill of the same name: `url-grammar`, `http-methods`,
`subdomain-architecture`, `authentication-authorization`, `service-discovery`.
