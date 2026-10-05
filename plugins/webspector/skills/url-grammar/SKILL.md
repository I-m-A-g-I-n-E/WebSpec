---
name: URL Grammar
description: This skill should be used when working with WebSpec URL patterns, REST path hierarchies, format suffixes, type resolution, EBNF grammar validation, or any URL routing that targets gimme.tools or WebSpec-compatible domains. Trigger phrases include "webspec url", "gimme.tools url", "REST path", "format suffix", "url validation", "path hierarchy".
version: 1.0.0
---

# WebSpec URL Grammar

> **Domain Note**: This skill uses `gimme.tools` as the default WebSpec domain. For self-hosted or enterprise deployments, substitute your configured domain.

## Overview

WebSpec URLs follow a clean REST-style pattern where every component has a single, unambiguous purpose. The grammar eliminates redundancy by ensuring the provider appears exactly once (in the subdomain) and hierarchy lives exclusively in the path.

## The Four Rules

WebSpec URLs follow four fundamental rules:

| Component | Purpose | Example |
|-----------|---------|---------|
| **Subdomain** | Identifies the provider | `slack`, `notion`, `gdrive` |
| **Path** | REST resource hierarchy | `/channels/C123/messages/M456` |
| **Suffix** | Content format | `.json`, `.md`, `.pdf` |
| **Query** | Filtering/pagination only | `?limit=50&after=M400` |

### Rule 1: Subdomain = Provider

The subdomain identifies which service handles the request. This is the ONLY place the provider appears.

```
slack.gimme.tools/...      → Slack API
notion.gimme.tools/...     → Notion API
gdrive.gimme.tools/...     → Google Drive API
github.gimme.tools/...     → GitHub API
api.gimme.tools/...        → Gateway (LLM-routed)
local.gimme.tools/...      → Local services
```

### Rule 2: Path = REST Hierarchy

Paths follow standard REST conventions with alternating collections and IDs:

```
/collection/id/collection/id...

/channels/C123/messages/M456
    ↑       ↑      ↑      ↑
  coll     id    coll    id
```

**Standard collections by provider:**

| Provider | Collections |
|----------|-------------|
| Slack | channels, messages, users, files, reactions |
| Notion | pages, blocks, databases, users |
| Google Drive | files, folders, permissions |
| Linear | teams, issues, projects, cycles |
| GitHub | repos, issues, pulls, contents, actions |

### Rule 3: Suffix = Format

Optional suffix specifies how to return content:

| Suffix | Returns | Use Case |
|--------|---------|----------|
| `.json` | Structured metadata | API responses, parsing |
| `.md` | Markdown content | Documentation, notes |
| `.pdf` | PDF export | Reports, sharing |
| `.html` | HTML render | Web display |
| `.txt` | Plain text | Simple content |
| `.csv` | CSV export | Data analysis |
| `.xml` | XML format | Legacy systems |
| (none) | Native/default | Provider decides |

### Rule 4: Query = Filtering Only

Query parameters handle filtering and pagination, NEVER hierarchy:

**Allowed:**
```
?state=open&assignee=me     # Filtering
?limit=50&after=M400        # Pagination
?q=quarterly+report         # Search
```

**Not Allowed:**
```
?channel=C123               # ❌ Use /channels/C123 instead
?file_id=abc                # ❌ Use /files/abc instead
```

## Complete URL Grammar (EBNF)

```ebnf
request        = method , SP , url ;
url            = "https://" , subdomain , ".gimme.tools" , path , [ format ] , [ query ] ;

subdomain      = provider | "api" | "local" ;
provider       = identifier ;

path           = { "/" , segment } ;
segment        = collection | id ;
collection     = identifier ;
id             = identifier | uuid | slug ;

format         = "." , format_type ;
format_type    = "json" | "md" | "pdf" | "html" | "txt" | "csv" | "xml" ;

query          = "?" , param , { "&" , param } ;
param          = key , "=" , value ;

method         = "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD" | "OPTIONS" ;

identifier     = letter , { letter | digit | "_" | "-" } ;
uuid           = 8*hexdig , "-" , 4*hexdig , "-" , 4*hexdig , "-" , 4*hexdig , "-" , 12*hexdig ;
slug           = { letter | digit | "-" } ;
```

## Validation Patterns

### Regex for URL Validation

```javascript
// Full URL pattern
const URL_PATTERN = /^https:\/\/([a-z][a-z0-9-]*)\.gimme\.tools(\/[a-zA-Z0-9_-]+)*(\.(json|md|pdf|html|txt|csv|xml))?(\?.*)?$/;

// Subdomain (provider)
const SUBDOMAIN_PATTERN = /^[a-z][a-z0-9-]*$/;

// Path segment
const SEGMENT_PATTERN = /^[a-zA-Z0-9_-]+$/;

// Format suffix
const FORMAT_PATTERN = /^\.(json|md|pdf|html|txt|csv|xml)$/;
```

### Validation Function

```javascript
function validateWebSpecUrl(url) {
  const parsed = new URL(url);

  // Check domain
  if (!parsed.hostname.endsWith('.gimme.tools')) {
    return { valid: false, error: 'Must use .gimme.tools domain' };
  }

  // Check subdomain is valid provider
  const subdomain = parsed.hostname.split('.')[0];
  if (!SUBDOMAIN_PATTERN.test(subdomain)) {
    return { valid: false, error: 'Invalid subdomain format' };
  }

  // Check path segments
  const segments = parsed.pathname.split('/').filter(Boolean);
  for (const seg of segments) {
    const cleanSeg = seg.replace(FORMAT_PATTERN, '');
    if (!SEGMENT_PATTERN.test(cleanSeg)) {
      return { valid: false, error: `Invalid path segment: ${seg}` };
    }
  }

  return { valid: true };
}
```

## Type Resolution Priority

When a URL doesn't specify a format suffix, WebSpec resolves the type through a priority chain:

1. **Explicit suffix** - `.json`, `.md`, etc.
2. **Type hint** - `Accept` header or `type` parameter
3. **Provider hint** - Provider's default for that resource
4. **Payload analysis** - Infer from content structure
5. **User default** - User's configured preference
6. **Prompt** - Ask user to specify

## Valid URL Examples

```bash
# Slack
GET slack.gimme.tools/channels
GET slack.gimme.tools/channels/C123
GET slack.gimme.tools/channels/C123/messages
GET slack.gimme.tools/channels/C123/messages/M456
GET slack.gimme.tools/channels/C123/messages?limit=50&after=M400
POST slack.gimme.tools/channels/general/messages

# Google Drive
GET gdrive.gimme.tools/files
GET gdrive.gimme.tools/files?q=quarterly+report
GET gdrive.gimme.tools/files/abc123
GET gdrive.gimme.tools/files/abc123.json
GET gdrive.gimme.tools/files/abc123.pdf
GET gdrive.gimme.tools/files/abc123.md
POST gdrive.gimme.tools/folders/xyz/files

# Notion
GET notion.gimme.tools/pages/xyz789
GET notion.gimme.tools/pages/xyz789.md
GET notion.gimme.tools/pages/xyz789/blocks
PATCH notion.gimme.tools/pages/xyz789/blocks/blk1

# Linear
GET linear.gimme.tools/teams/ENG/issues
GET linear.gimme.tools/teams/ENG/issues?state=in_progress
GET linear.gimme.tools/issues/LIN-42
POST linear.gimme.tools/teams/ENG/issues
DELETE linear.gimme.tools/issues/LIN-42

# GitHub
GET github.gimme.tools/repos/owner/name
GET github.gimme.tools/repos/owner/name/issues
GET github.gimme.tools/repos/owner/name/issues/42
GET github.gimme.tools/repos/owner/name/contents/src/main.ts
```

## Invalid URL Patterns

```bash
# ❌ WRONG: Provider in path (redundant)
GET slack.gimme.tools/message.slack
GET gdrive.gimme.tools/file.gdrive/abc123
POST slack.gimme.tools/message.slack?channel=general

# ✅ CORRECT: Provider only in subdomain
GET slack.gimme.tools/channels/C123/messages/M456
GET gdrive.gimme.tools/files/abc123
POST slack.gimme.tools/channels/general/messages

# ❌ WRONG: Hierarchy in query params
GET slack.gimme.tools/messages?channel=C123

# ✅ CORRECT: Hierarchy in path
GET slack.gimme.tools/channels/C123/messages
```

## Common Validation Issues

| Issue | Problem | Fix |
|-------|---------|-----|
| Provider redundancy | Provider in both subdomain and path | Remove from path |
| Hierarchy in query | Resource ID in query params | Move to path |
| Invalid format suffix | Unsupported format like `.xml2` | Use standard suffixes |
| Missing HTTPS | Using HTTP protocol | Always use HTTPS |
| Invalid path segments | Special characters in path | Use URL-safe characters |
