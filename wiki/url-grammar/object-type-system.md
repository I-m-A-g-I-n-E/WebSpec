# Object Type System

The object type system organizes resources into standard collections with format suffixes for content negotiation.

## Core Insight: Subdomain IS the Provider

With the clean URL design, we don't need `.provider` in the path -- the subdomain already tells us:

```
slack.gimme.tools/channels/C123/messages/M456
      ^
   provider is slack (no need to say it again!)
```

Format suffixes handle **how** content is returned, not **where** it comes from:

```bash
gdrive.gimme.tools/files/abc123        # native format
gdrive.gimme.tools/files/abc123.json   # metadata as JSON
gdrive.gimme.tools/files/abc123.md     # content as Markdown
gdrive.gimme.tools/files/abc123.pdf    # content as PDF
```

---

## Format Suffixes (Content Negotiation)

Format suffixes specify **how** to return content, following familiar file extension conventions:

| Suffix | Purpose | Example |
|--------|---------|---------|
| .json | Metadata/structured data | `/files/abc.json` |
| .pdf | PDF format | `/documents/xyz.pdf` |
| .md | Markdown | `/pages/123.md` |
| .html | HTML render | `/pages/123.html` |
| .txt | Plain text | `/documents/xyz.txt` |
| .csv | Comma-separated values | `/spreadsheets/abc.csv` |
| .xml | XML data | `/data/records.xml` |
| (none) | Native/default format | `/files/abc` |

### Format Suffix Examples

```bash
# Get Notion page in different formats
GET notion.gimme.tools/pages/xyz789         # native Notion format
GET notion.gimme.tools/pages/xyz789.md      # as Markdown
GET notion.gimme.tools/pages/xyz789.html    # as HTML
GET notion.gimme.tools/pages/xyz789.json    # metadata only

# Get Google Drive file in different formats
GET gdrive.gimme.tools/files/abc123         # native format
GET gdrive.gimme.tools/files/abc123.pdf     # export as PDF
GET gdrive.gimme.tools/files/abc123.txt     # extract plain text
```

---

## Standard Collections by Provider

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

This enables intuitive URL construction:

```bash
# Navigate the hierarchy naturally
GET slack.gimme.tools/channels                            # list channels
GET slack.gimme.tools/channels/C123                       # channel info
GET slack.gimme.tools/channels/C123/messages              # messages in channel
GET slack.gimme.tools/channels/C123/messages/M456         # specific message
GET slack.gimme.tools/channels/C123/messages/M456/threads # thread replies
```

---

## Conversion via Format Suffix

Format suffixes enable natural conversion operations:

```bash
# Convert Notion page to Markdown
GET notion.gimme.tools/pages/xyz789.md

# Export spreadsheet as CSV
GET gdrive.gimme.tools/files/spreadsheet123.csv

# Get PDF export of a doc
GET gdrive.gimme.tools/files/doc456.pdf

# Get rendered HTML
GET notion.gimme.tools/pages/xyz789.html
```

The format suffix tells the server **how** to return the content, leveraging each provider's native export capabilities.

Each provider defines its own collection hierarchy. Resources are organized into standard collections (channels, files, pages, issues) with format suffixes (.json, .md, .pdf) for content negotiation.
