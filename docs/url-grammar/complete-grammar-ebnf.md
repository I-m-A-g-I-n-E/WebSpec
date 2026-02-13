# Complete Grammar (EBNF)

The complete formal grammar for WebSpec URLs in Extended Backus-Naur Form.

## URL Grammar

```ebnf
(* WebSpec URL Grammar *)
(* Clean REST-style URLs: subdomain = provider, path = hierarchy, suffix = format *)

request        = method , SP , url ;
url            = "https://" , subdomain , ".gimme.tools" , path , [ format ] , [ query ] ;

(* Subdomain identifies the provider - no redundancy in path *)
subdomain      = provider | "api" | "local" ;
provider       = identifier ;   (* slack, notion, gdrive, linear, github, etc. *)

(* Path follows REST hierarchy: /collection/id/collection/id... *)
path           = { "/" , segment } ;
segment        = collection | id ;
collection     = identifier ;   (* channels, messages, files, pages, issues, etc. *)
id             = identifier | uuid | slug ;

(* Format suffix for content negotiation *)
format         = "." , format_type ;
format_type    = "json" | "md" | "pdf" | "html" | "txt" | "csv" | "xml" ;

(* Query params for filtering/pagination only - never hierarchy *)
query          = "?" , param , { "&" , param } ;
param          = key , "=" , value ;
key            = identifier ;
value          = { urlchar } ;

(* HTTP Methods *)
method         = "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD" | "OPTIONS" ;

(* Terminals *)
identifier     = letter , { letter | digit | "_" | "-" } ;
uuid           = 8*hexdig , "-" , 4*hexdig , "-" , 4*hexdig , "-" , 4*hexdig , "-" , 12*hexdig ;
slug           = { letter | digit | "-" } ;
letter         = "a" | ... | "z" | "A" | ... | "Z" ;
digit          = "0" | ... | "9" ;
hexdig         = digit | "a" | ... | "f" | "A" | ... | "F" ;
urlchar        = letter | digit | "-" | "_" | "." | "~" | "%" , hexdig , hexdig ;
SP             = " " ;
```

---

## Production Rules Explained

### Subdomain (Provider)

The subdomain identifies **which service** handles the request. This is the **only** place the provider appears.

```ebnf
subdomain = provider | "api" | "local" ;
```

| Subdomain | Purpose |
|-----------|---------|
| `slack` | Slack API |
| `notion` | Notion API |
| `gdrive` | Google Drive API |
| `linear` | Linear API |
| `github` | GitHub API |
| `api` | Gateway (LLM-routed) |
| `local` | Local services |

### Path (REST Hierarchy)

The path follows standard REST conventions with alternating collections and IDs:

```ebnf
path = { "/" , segment } ;
segment = collection | id ;
```

**Pattern:** `/collection/id/collection/id...`

```
/channels/C123/messages/M456
    ^       ^      ^      ^
  coll     id    coll    id
```

### Format Suffix (Content Negotiation)

Optional suffix specifying **how** to return content:

```ebnf
format = "." , format_type ;
format_type = "json" | "md" | "pdf" | "html" | "txt" | "csv" | "xml" ;
```

| Suffix | Returns |
|--------|---------|
| `.json` | Metadata/structured data |
| `.md` | Markdown content |
| `.pdf` | PDF export |
| `.html` | HTML render |
| `.txt` | Plain text |
| `.csv` | CSV export |
| `.xml` | XML format |
| (none) | Native/default |

### Query Parameters (Filtering Only)

Query params handle **filtering and pagination**, never hierarchy:

```ebnf
query = "?" , param , { "&" , param } ;
```

**Allowed uses:**

- Filtering: `?state=open&assignee=me`
- Pagination: `?limit=50&after=M400`
- Search: `?q=quarterly+report`

**Not allowed:**

- Hierarchy: `?channel=C123` (use `/channels/C123` instead)

---

## Examples

### Valid URLs

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

# Gateway (LLM-routed)
POST api.gimme.tools/execute
```

### Invalid URLs (Old Syntax)

```bash
# WRONG: Provider in path (redundant)
GET slack.gimme.tools/message.slack
GET gdrive.gimme.tools/file.gdrive/abc123
POST slack.gimme.tools/message.slack?channel=general

# CORRECT: Provider only in subdomain
GET slack.gimme.tools/channels/C123/messages/M456
GET gdrive.gimme.tools/files/abc123
POST slack.gimme.tools/channels/general/messages
```

---

## Grammar Validation

### Regex Patterns

```javascript
// Full URL pattern
const URL_PATTERN = /^https:\/\/([a-z][a-z0-9-]*)\.gimme\.tools(\/[a-zA-Z0-9_-]+)*(\.(?:json|md|pdf|html|txt|csv|xml))?(\?.*)?$/;

// Subdomain (provider)
const SUBDOMAIN_PATTERN = /^[a-z][a-z0-9-]*$/;

// Path segment
const SEGMENT_PATTERN = /^[a-zA-Z0-9_-]+$/;

// Format suffix
const FORMAT_PATTERN = /^\.(json|md|pdf|html|txt|csv|xml)$/;
```

### Validation Examples

```javascript
function validateWebSpecUrl(url) {
  const parsed = new URL(url);

  // Check domain
  if (!parsed.hostname.endsWith('.gimme.tools')) {
    return { valid: false, error: 'Must use .gimme.tools domain' };
  }

  // Check subdomain is provider
  const subdomain = parsed.hostname.split('.')[0];
  if (!SUBDOMAIN_PATTERN.test(subdomain)) {
    return { valid: false, error: 'Invalid subdomain' };
  }

  // Check path segments
  const segments = parsed.pathname.split('/').filter(Boolean);
  for (const seg of segments) {
    // Strip format suffix from last segment
    const cleanSeg = seg.replace(FORMAT_PATTERN, '');
    if (!SEGMENT_PATTERN.test(cleanSeg)) {
      return { valid: false, error: `Invalid path segment: ${seg}` };
    }
  }

  return { valid: true };
}
```

---

## See Also

- [Core Syntax](core-syntax.md) -- Human-readable overview
- [Object Type System](object-type-system.md) -- Standard collections by provider
- [Type Resolution & Inference](type-resolution-inference.md) -- How incomplete URLs are resolved
