# Webspector

A Claude Code plugin for validating code against WebSpec protocol standards.

## Overview

Webspector provides specialized knowledge and proactive validation agents for the WebSpec protocol, covering:

- **URL Grammar** - REST URL structure, format suffixes, EBNF grammar validation
- **HTTP Methods** - Method semantics, permission scoping, prompt injection defense
- **Subdomain Architecture** - Isolation model, cookie/token scoping, local bridge
- **Authentication & Authorization** - OAuth flows, JWT tokens, keychain integration
- **Service Discovery** - NLP resolution, embedding schema, three-way join algorithm

## Installation

Add this plugin to your Claude Code configuration:

```bash
claude plugins add ./webspector
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

Quick check of a specific standard area.

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

Webspector includes proactive agents that automatically validate code when relevant changes are detected:

| Agent | Triggers On |
|-------|-------------|
| `url-grammar-validator` | URL/endpoint code, route definitions |
| `http-methods-reviewer` | API implementations, HTTP handlers |
| `subdomain-security-analyzer` | Auth-related code, token/cookie handling |
| `auth-flow-auditor` | OAuth implementations, auth code changes |
| `service-discovery-reviewer` | NLP/embedding code, discovery logic |

## Configuration

### Domain Whitelist

Webspector uses `gimme.tools` as the default WebSpec domain. For self-hosted or enterprise deployments, configure approved domains in `.claude/webspector.local.md`:

```markdown
---
allow_custom_domains: true
require_https: true
admin_write_access: false
---

# Webspector Configuration

## Approved WebSpec Domains

These domains are approved for WebSpec protocol validation:

- gimme.tools (default, public)
- internal.company.com (enterprise)
- dev.company.com (development)
```

### Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `allow_custom_domains` | `true` | Allow domains beyond gimme.tools |
| `require_https` | `true` | Require HTTPS for all WebSpec URLs |
| `admin_write_access` | `false` | Restrict config writes to admins |

## WebSpec Protocol Reference

This plugin validates compliance with the WebSpec protocol standards:

### The Four Rules

1. **Subdomain = Provider** - `slack.gimme.tools`, `notion.gimme.tools`
2. **Path = REST Hierarchy** - `/collection/id/collection/id...`
3. **Suffix = Format** - `.json`, `.md`, `.pdf`, `.html`, `.txt`, `.csv`, `.xml`
4. **Query = Filtering** - `?limit=50&after=M400` (never hierarchy)

### URL Grammar

```
https://{subdomain}.gimme.tools/{path}[.{format}][?{query}]
```

Valid examples:
```
GET slack.gimme.tools/channels/C123/messages
GET gdrive.gimme.tools/files/abc123.pdf
POST notion.gimme.tools/pages/xyz789/blocks
```

### HTTP Method Semantics

| Method | Semantic | Absorbed Verbs |
|--------|----------|----------------|
| GET | read | fetch, retrieve, list, show, view |
| POST | create | add, insert, new, submit, send |
| PUT | replace | set, overwrite |
| PATCH | update | modify, change, edit |
| DELETE | remove | trash, archive, destroy |

### Permission Scoping

```
SCOPE := METHOD ":" HOST? PATH_PATTERN
```

Examples:
- `GET:slack.gimme.tools/channels/*` - Read any channel
- `POST:*/messages` - Create messages on any provider
- `*:gdrive.gimme.tools/files/shared/*` - Full access to shared files

## License

MIT
