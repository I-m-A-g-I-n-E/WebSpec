# Webspector

A Claude Code plugin that checks code against the WebSpec spec
(<https://i-m-a-g-i-n-e.github.io/WebSpec/spec/>). It reviews MCP servers whose tools a WebSpec
gateway routes, clients and harness shims that call a gateway, and gateway and proxy
configuration. Each finding cites the rule ID it rests on, such as `PA-4` or `DF-1`.

## Overview

| Area (skill) | Rules | Covers |
|---|---|---|
| **URL Grammar** (`url-grammar`) | HG, PA, AR | Destination hosts, the route rule and route map, single-segment paths, arguments |
| **HTTP Methods** (`http-methods`) | TC, MB, DF, RQ, RS | Tool contracts and annotations, method binding, `WebSpec-Definer`, response headers |
| **Subdomain Architecture** (`subdomain-architecture`) | HG, DP-5, DP-8 | Destinations and qualifiers, public exposure, proxy forwarding, CORS |
| **Authentication & Authorization** (`authentication-authorization`) | LV, GD, ID, CL, AP, CO, SH | Levels 0 to 4: guard and nonce, `Idempotency-Key`, clearance, human approval, key custody |
| **Service Discovery** (`service-discovery`) | DS, PA-2, SH-2 | `GET /`, `OPTIONS`, `HEAD`, building requests from the route map |

## Installation

Add this plugin to your Claude Code configuration:

```bash
claude plugins add ./plugins/webspector
```

## Commands

### `/webspector:validate`

Run a full WebSpec compliance check on the current codebase.

```bash
/webspector:validate                     # Concise summary
/webspector:validate --verbose           # Detailed report with spec references
/webspector:validate --action=suggest    # Provide fix suggestions
/webspector:validate --action=revise     # Auto-fix issues (with confirmation)
```

### `/webspector:check <area>`

Quick check of a specific area.

```bash
/webspector:check url-grammar            # or: url
/webspector:check http-methods           # or: http
/webspector:check subdomain-architecture # or: subdomain
/webspector:check authentication-authorization  # or: auth
/webspector:check service-discovery      # or: discovery
```

**Action flags:**
- `--action=comment` - Add inline comments to code
- `--action=suggest` - Provide suggestions without modifying
- `--action=revise` - Automatically fix issues (with confirmation)
- `--verbose` - Include full explanations with spec references

## Proactive Agents

| Agent | Triggers On |
|-------|-------------|
| `url-grammar-validator` | Requests built for a WebSpec gateway, MCP tool names and annotations, `labels` and `bindings` config |
| `http-methods-reviewer` | MCP tool annotations, method and `WebSpec-Definer` choice, WebSpec response handling |
| `subdomain-security-analyzer` | Destination and proxy config, `WEBSPEC_CORS_ORIGINS`, `WEBSPEC_HOST` |
| `auth-flow-auditor` | Nonce, guard, `Idempotency-Key`, clearance, and approval code; guard-key custody |
| `service-discovery-reviewer` | `GET /`, `OPTIONS`, and `HEAD` use; building requests from the route map |

## WebSpec in brief

A request names its destination in the host, its tool in the method and a single-segment
noun path, and on `POST` its action in `WebSpec-Definer`:

```text
METHOD https://{destination}.{domain}/{noun}?{arguments}
```

The domain is the gateway's (`WEBSPEC_DOMAIN`), or `localhost` for loopback names such as
`notes.localhost`. The route rule (`PA-4`) gives each MCP tool one entry from its name and
contract: `read_note` → `GET /note`, `delete_note` → `DELETE /note`, `send_email` →
`POST /email` with `WebSpec-Definer: SEND`, and a tool whose name or contract doesn't fit →
`/{name}`. The gateway refuses a method the tool's contract doesn't admit (`405` with
`Allow`), a `POST` without a definer (`400 missing_definer`), and a change whose definer
belongs to another method (`400 definer_family_mismatch`). Levels 1 to 4 add a guard HMAC
with a single-use nonce, an `Idempotency-Key`, a clearance, and a person's approval.

Every header WebSpec defines is `WebSpec-<Name>`. Earlier names (`X-Gimme-Definer`,
`X-WebSpec-*`, `X-UFO-*`) are not recognized.

## What this plugin no longer checks

Earlier versions of this plugin were written against a design that predates the current
spec: a fixed `gimme.tools` domain, REST hierarchies in the path, format suffixes, METHOD
tokenization, `METHOD:host/path` scopes, OAuth and JWT flows, and NLP-based service
discovery. None of these is in the spec, and the plugin no longer reports their absence.

## License

MIT
