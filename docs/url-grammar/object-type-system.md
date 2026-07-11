# Object Type System

> **Status: Proposed (C).** This page is the examples appendix for the REST-hierarchy grammar in
> [Complete Grammar (EBNF)](complete-grammar-ebnf.md) — collections, IDs, and format suffixes are
> not implemented today. The current gateway routes by subdomain + literal MCP tool name; see the
> [status matrix](../index.md#status) and [ROADMAP-C.md](../ROADMAP-C.md). Format-suffix rules
> live in [Complete Grammar (EBNF) → Format Suffix](complete-grammar-ebnf.md#format-suffix-content-negotiation);
> this page only holds the provider-by-provider collection tables.

## Examples: Standard Collections by Provider

Each provider defines its own collection hierarchy under the proposed REST-hierarchy grammar.
Resources are organized into standard collections (channels, files, pages, issues) with format
suffixes (`.json`, `.md`, `.pdf`) for content negotiation.

### Communication (slack, discord, teams)

| Collection | Description | Example Path |
|------------|-------------|--------------|
| channels | Communication channels | `/channels/C123` |
| messages | Messages within channels | `/channels/C123/messages/M456` |
| threads | Thread replies | `/channels/C123/messages/M456/threads` |
| users | Workspace members | `/users/U123` |
| reactions | Emoji reactions | `/channels/C123/messages/M456/reactions` |

### Content (gdrive, dropbox, notion)

| Collection | Description | Example Path |
|------------|-------------|--------------|
| files | Generic files | `/files/xyz123` |
| folders | File containers | `/folders/abc/files` |
| pages | Structured documents | `/pages/xyz789` |
| blocks | Content blocks within pages | `/pages/xyz/blocks/blk1` |
| databases | Structured data collections | `/databases/db123/rows` |

### Productivity (linear, asana, todoist)

| Collection | Description | Example Path |
|------------|-------------|--------------|
| issues | Work items/tasks | `/issues/LIN-42` |
| projects | Issue containers | `/projects/p1/issues` |
| teams | Team groupings | `/teams/ENG/issues` |
| cycles | Sprint/iteration | `/teams/ENG/cycles/current` |
| comments | Issue comments | `/issues/LIN-42/comments` |

### Calendar (gcal, outlook)

| Collection | Description | Example Path |
|------------|-------------|--------------|
| calendars | Calendar containers | `/calendars/primary` |
| events | Calendar entries | `/calendars/primary/events/evt123` |
| attendees | Event participants | `/events/evt123/attendees` |

### Code (github, gitlab)

| Collection | Description | Example Path |
|------------|-------------|--------------|
| repos | Repositories | `/repos/owner/name` |
| issues | Repository issues | `/repos/owner/name/issues/42` |
| pulls | Pull requests | `/repos/owner/name/pulls/123` |
| commits | Git commits | `/repos/owner/name/commits/abc123` |
| contents | File contents | `/repos/owner/name/contents/src/main.ts` |

---

## Collection Hierarchy

Collections can nest to form natural hierarchies:

```yaml
channels:
  children:
    - messages:
        children:
          - threads
          - reactions

pages:
  children:
    - blocks
    - comments

repos:
  children:
    - issues
    - pulls
    - commits
    - contents
```

This enables intuitive URL construction under the proposed grammar:

```bash
# Navigate the hierarchy naturally (Proposed C)
GET slack.gimme.tools/channels                            # list channels
GET slack.gimme.tools/channels/C123                       # channel info
GET slack.gimme.tools/channels/C123/messages              # messages in channel
GET slack.gimme.tools/channels/C123/messages/M456         # specific message
GET slack.gimme.tools/channels/C123/messages/M456/threads # thread replies
```
