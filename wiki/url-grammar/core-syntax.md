# Core Syntax

The complete WebSpec URL grammar:

## URL Structure

```
{subdomain}.gimme.tools/{collection}/{id}/{collection}/{id}...{.format}
```

### Design Principle: No Redundancy

The subdomain IS the provider, so having `.provider` in the path is redundant:

| Before (redundant) | After (clean) |
|--------------------|---------------|
| `slack.gimme.tools/message.slack` | `slack.gimme.tools/channels/C123/messages/M456` |
| `gdrive.gimme.tools/file.gdrive/abc123` | `gdrive.gimme.tools/files/abc123` |
| `notion.gimme.tools/document.notion/xyz` | `notion.gimme.tools/pages/xyz/blocks/def` |

---

## The Four Rules

### 1. Subdomain = Provider (routing/isolation)

The subdomain identifies **which service** handles the request and creates an **isolation boundary**. Each subdomain is a separate security origin.

```
slack.gimme.tools    -> Slack API
notion.gimme.tools   -> Notion API
gdrive.gimme.tools   -> Google Drive API
linear.gimme.tools   -> Linear API
github.gimme.tools   -> GitHub API
```

See Isolation Model for security implications.

### 2. Path = REST Hierarchy (familiar, standard)

The path follows standard REST conventions with nested collections and IDs:

```
/channels/C123/messages/M456     -> specific message in channel
/pages/abc/blocks/def            -> specific block in page
/files/xyz123                    -> specific file
/repos/owner/name/issues/42      -> specific issue in repo
/workspaces/ws1/projects/p1      -> specific project in workspace
```

### 3. Suffix = Format (content negotiation)

Optional format suffix specifies how to return the content:

```
/files/xyz123.pdf      -> file content as PDF
/files/xyz123.json     -> metadata as JSON
/files/xyz123.md       -> content as Markdown
/files/xyz123          -> native/default format
```

| Suffix | Purpose | Example |
|--------|---------|---------|
| .json | Metadata/structured data | `/files/abc.json` |
| .pdf | PDF format | `/documents/xyz.pdf` |
| .md | Markdown | `/pages/123.md` |
| .html | HTML render | `/pages/123.html` |
| .txt | Plain text | `/documents/xyz.txt` |
| (none) | Native/default | `/files/abc` |

### 4. Query Params = Filtering/Pagination (not hierarchy)

Query parameters handle filtering, pagination, and operation arguments -- never hierarchy:

```
/channels/C123/messages?limit=50&after=M400
/files?q=quarterly+report&type=pdf
/issues?state=open&assignee=me
```

---

## Components

### METHOD (Verb)

The HTTP method serves as both the **semantic verb** and the **permission boundary**. See Method Semantics for complete mapping.

| Method | Semantic Role |
|--------|---------------|
| GET | Read, retrieve, search, list |
| POST | Create, send, invoke, execute |
| PUT | Replace, overwrite |
| PATCH | Update, modify, append |
| DELETE | Remove, revoke, cancel |
| HEAD | Check existence, get metadata |
| OPTIONS | Discover capabilities |

### service (Subdomain)

The subdomain identifies the **service provider** and creates an **isolation boundary**:

```
slack.gimme.tools
notion.gimme.tools
gdrive.gimme.tools
linear.gimme.tools
```

### collection (Noun, plural)

The primary noun representing **what** is being acted upon. Standard collections:

| Collection | Description | Example Path |
|------------|-------------|--------------|
| messages | Communication units | `/channels/C123/messages` |
| files | Binary or text content | `/files/xyz123` |
| pages | Structured documents | `/pages/abc/blocks` |
| events | Calendar/scheduling items | `/calendars/cal1/events` |
| issues | Work items, tasks | `/projects/p1/issues/42` |
| repos | Code repositories | `/repos/owner/name` |

### id (Instance)

Instance identifier for operations on specific resources:

```
GET    slack.gimme.tools/channels/C123/messages/M456
PATCH  notion.gimme.tools/pages/xyz789/blocks/blk1
DELETE linear.gimme.tools/issues/LIN-42
```

---

## Gateway Behavior

At `api.gimme.tools`, we don't use paths for routing -- the LLM does:

```json
POST api.gimme.tools/execute
Content-Type: application/json

{
  "intent": "get the quarterly report from my Drive",
  "thread_pub_key": "..."
}
```

The gateway:

1. LLM interprets intent
2. Decides: `provider=gdrive`, `method=GET`, `path=/files?q=quarterly+report`
3. Routes to: `gdrive.gimme.tools/files?q=quarterly+report`

---

## Examples

### Slack

```bash
# List channels
GET slack.gimme.tools/channels

# Get messages in a channel
GET slack.gimme.tools/channels/C123/messages?limit=50

# Send a message
POST slack.gimme.tools/channels/general/messages
Content-Type: application/json
{"text": "Hello team!"}

# Get specific message
GET slack.gimme.tools/channels/C123/messages/M456
```

### Google Drive

```bash
# List files
GET gdrive.gimme.tools/files?q=quarterly+report

# Get file metadata
GET gdrive.gimme.tools/files/abc123.json

# Download as PDF
GET gdrive.gimme.tools/files/abc123.pdf

# Download as Markdown
GET gdrive.gimme.tools/files/abc123.md

# Upload file
POST gdrive.gimme.tools/folders/folder123/files
```

### Notion

```bash
# Get page content
GET notion.gimme.tools/pages/xyz789

# Get page as Markdown
GET notion.gimme.tools/pages/xyz789.md

# List blocks in page
GET notion.gimme.tools/pages/xyz789/blocks

# Update specific block
PATCH notion.gimme.tools/pages/xyz789/blocks/blk1
```

### Linear

```bash
# List issues
GET linear.gimme.tools/teams/ENG/issues?state=in_progress

# Create issue
POST linear.gimme.tools/teams/ENG/issues

# Update issue
PATCH linear.gimme.tools/issues/LIN-42

# Delete issue
DELETE linear.gimme.tools/issues/LIN-42
```

---

## Formal Grammar (Simplified)

```ebnf
REQUEST    := METHOD " " URL
URL        := "https://" SUBDOMAIN ".gimme.tools" PATH [FORMAT] [QUERY]
SUBDOMAIN  := provider
PATH       := ("/" collection ["/" id])+
FORMAT     := "." format_type
QUERY      := "?" param ("&" param)*
```

See [Complete Grammar (EBNF)](complete-grammar-ebnf.md) for full specification.
