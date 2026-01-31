---
name: Service Discovery
description: This skill should be used when working with WebSpec service discovery, NLP-driven resolution, embedding schemas, the three-way join algorithm, or tool matching logic. Trigger phrases include "service discovery", "nlp resolution", "three-way join", "tool matching", "semantic search", "embedding schema", "intent extraction".
version: 1.0.0
---

# WebSpec Service Discovery

> **Domain Note**: This skill uses `gimme.tools` as the default WebSpec domain. For self-hosted or enterprise deployments, substitute your configured domain.

## Overview

WebSpec service discovery matches natural language intent with available tools using a three-way join algorithm. The system combines NLP extraction, embedding-based semantic search, and user context to rank tools by relevance and accessibility.

## NLP-Driven Resolution

### Intent Extraction

User requests are parsed into structured intent with predicate-object pairs:

```
"post the quarterly numbers to the finance channel"
     ↓
┌─────────────────────────────────────────────────────┐
│ Intent Extraction                                    │
│                                                      │
│ predicate: "post"                                    │
│ object: "numbers" (type: data/spreadsheet)          │
│ destination: "finance channel"                       │
│ hints:                                               │
│   - channel_name: "finance"                          │
│   - content_type: "quarterly report"                 │
│   - action_category: "share/send"                    │
└─────────────────────────────────────────────────────┘
```

### Extraction Algorithm

```javascript
function extractIntent(userRequest) {
  // 1. Identify the main verb (predicate)
  const predicate = extractPredicate(userRequest);

  // 2. Identify the direct object
  const object = extractObject(userRequest);

  // 3. Extract destination/target
  const destination = extractDestination(userRequest);

  // 4. Gather contextual hints
  const hints = extractHints(userRequest);

  return {
    predicate: normalizePredicate(predicate),
    object: normalizeObject(object),
    destination,
    hints,
    raw: userRequest
  };
}

function normalizePredicate(verb) {
  // Map to HTTP method categories
  const verbMappings = {
    'read': ['get', 'fetch', 'retrieve', 'list', 'show', 'view', 'check', 'see'],
    'create': ['post', 'send', 'add', 'create', 'new', 'make', 'submit', 'write'],
    'update': ['edit', 'modify', 'change', 'update', 'patch', 'revise'],
    'replace': ['set', 'put', 'overwrite', 'replace'],
    'delete': ['delete', 'remove', 'trash', 'archive', 'destroy']
  };

  for (const [category, verbs] of Object.entries(verbMappings)) {
    if (verbs.includes(verb.toLowerCase())) {
      return { verb, category, method: categoryToMethod(category) };
    }
  }

  return { verb, category: 'unknown', method: null };
}
```

### Object Normalization

```javascript
function normalizeObject(obj) {
  // Map to standard object types
  const objectMappings = {
    'message': ['message', 'msg', 'text', 'note', 'notification'],
    'file': ['file', 'document', 'doc', 'attachment', 'upload'],
    'page': ['page', 'document', 'wiki', 'article'],
    'issue': ['issue', 'ticket', 'bug', 'task', 'item'],
    'channel': ['channel', 'room', 'chat', 'conversation']
  };

  for (const [type, synonyms] of Object.entries(objectMappings)) {
    if (synonyms.includes(obj.toLowerCase())) {
      return { raw: obj, normalized: type };
    }
  }

  return { raw: obj, normalized: obj.toLowerCase() };
}
```

## Embedding Schema

### Multi-Vector Representation

Each registered tool has multiple embeddings for robust matching:

```yaml
tool:
  id: "slack-send-message"
  route: "POST slack.gimme.tools/messages"

  embeddings:
    # Primary semantic description
    canonical:
      text: "send a message via Slack to a channel or user"
      vector: [0.12, -0.34, 0.56, ...]

    # Predicate variations
    predicates:
      - text: "send message"
        vector: [0.11, -0.33, 0.55, ...]
      - text: "post to slack"
        vector: [0.13, -0.35, 0.57, ...]

    # Object type focus
    objects:
      - text: "slack message notification"
        vector: [0.14, -0.32, 0.54, ...]

    # Contextual examples
    examples:
      - text: "notify the team in the general channel"
        vector: [0.15, -0.31, 0.53, ...]
      - text: "send an update to engineering"
        vector: [0.16, -0.30, 0.52, ...]
```

### Embedding Generation

```python
def generate_tool_embeddings(tool):
    embeddings = {}

    # Canonical description
    embeddings['canonical'] = embed(tool.semantics.canonical)

    # Predicate variations
    embeddings['predicates'] = [
        embed(f"{pred} {tool.primary_object}")
        for pred in tool.semantics.predicates
    ]

    # Object variations
    embeddings['objects'] = [
        embed(f"{tool.provider} {obj}")
        for obj in tool.semantics.objects
    ]

    # Example queries
    embeddings['examples'] = [
        embed(example)
        for example in tool.semantics.example_queries
    ]

    return embeddings
```

### Vector Search

```python
def semantic_search(query, top_k=20):
    query_embedding = embed(query)

    # Multi-vector search with weighted aggregation
    results = vector_db.search(
        query_vector=query_embedding,
        index="tools",
        top_k=top_k * 2,  # Over-fetch for re-ranking
        aggregation={
            'canonical': 1.0,
            'predicates': 0.8,
            'objects': 0.7,
            'examples': 0.6
        }
    )

    # Deduplicate by tool ID, keeping highest score
    unique_tools = {}
    for result in results:
        tool_id = result.tool_id
        if tool_id not in unique_tools or result.score > unique_tools[tool_id].score:
            unique_tools[tool_id] = result

    return sorted(unique_tools.values(), key=lambda r: r.score, reverse=True)[:top_k]
```

## The Three-Way Join

### Data Sources

```
┌─────────────────────────────────────────────────────────────┐
│                       USER REQUEST                           │
│    "post the quarterly numbers to the finance channel"       │
└──────────────────────────┬──────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
┌──────────────────┐ ┌────────────────┐ ┌────────────────┐
│  CONNECTED       │ │  REGISTRY      │ │  KEYCHAIN      │
│  SERVICES        │ │                │ │  HINTS         │
│                  │ │ All available  │ │                │
│ OAuth tokens     │ │ tools with     │ │ Domains user   │
│ already issued   │ │ embeddings     │ │ has creds for  │
└────────┬─────────┘ └───────┬────────┘ └───────┬────────┘
         │                   │                   │
         └───────────────────┼───────────────────┘
                             │
                             ▼
                    ┌────────────────────┐
                    │   THREE-WAY JOIN   │
                    └────────────────────┘
```

### Join Algorithm

```python
def three_way_join(user_request, user):
    # 1. Extract intent from request
    intent = nlp_extract(user_request)
    query_embedding = embed(intent)

    # 2. Semantic search against registry
    registry_matches = vector_search(
        embedding=query_embedding,
        index="tools",
        top_k=20
    )

    # 3. Classify each match by user's relationship
    results = []
    for tool in registry_matches:
        status = classify_tool(tool, user)
        results.append({
            'tool': tool,
            'status': status,
            'score': tool.similarity * status.weight
        })

    # 4. Sort by weighted score
    return sorted(results, key=lambda r: r['score'], reverse=True)


def classify_tool(tool, user):
    service = tool.service

    # Already authorized?
    if service in user.connected_services:
        return Status.CONNECTED  # weight: 1.0

    # In user's keychain?
    if any(d in user.keychain_domains for d in service.canonical_domains):
        return Status.KEYCHAIN   # weight: 0.8

    # Available but not connected?
    if service.available:
        return Status.AVAILABLE  # weight: 0.5

    return Status.UNAVAILABLE    # weight: 0.0
```

### Result Categories

| Status | Description | Weight | UX Action |
|--------|-------------|--------|-----------|
| CONNECTED | OAuth already authorized | 1.0 | "Use this" button |
| KEYCHAIN | Found in password manager | 0.8 | "Connect with FaceID" |
| AVAILABLE | In registry, not connected | 0.5 | "Sign up / Connect" |
| UNAVAILABLE | Not in registry | 0.0 | Don't show |

### Scoring Formula

```
final_score = semantic_similarity × status_weight × recency_boost × preference_boost
```

| Factor | Range | Description |
|--------|-------|-------------|
| semantic_similarity | 0.0 - 1.0 | Cosine similarity from vector search |
| status_weight | 0.0 - 1.0 | Based on connection status |
| recency_boost | 1.0 - 1.2 | Higher if user used this tool recently |
| preference_boost | 1.0 - 1.3 | Higher if user marked as preferred |

## Example Walkthrough

### Input Request

```
"post the quarterly numbers to finance"
```

### Step 1: Intent Extraction

```json
{
  "predicate": "post",
  "object": "numbers",
  "destination": "finance",
  "hints": {
    "type": "data/spreadsheet",
    "channel": "finance"
  }
}
```

### Step 2: Registry Search

| Tool | Semantic Similarity |
|------|---------------------|
| POST slack.gimme.tools/messages | 0.85 |
| POST slack.gimme.tools/files | 0.82 |
| POST teams.gimme.tools/messages | 0.78 |
| POST gdrive.gimme.tools/files | 0.65 |
| POST gsheets.gimme.tools/spreadsheets | 0.60 |

### Step 3: User Classification

```yaml
User's connected services:
  - slack.gimme.tools
  - gdrive.gimme.tools

User's keychain hints:
  - teams.microsoft.com
  - notion.so
```

### Step 4: Final Ranking

| Tool | Similarity | Status | Weight | Final |
|------|------------|--------|--------|-------|
| POST slack.gimme.tools/messages | 0.85 | CONNECTED | 1.0 | **0.85** |
| POST slack.gimme.tools/files | 0.82 | CONNECTED | 1.0 | **0.82** |
| POST gdrive.gimme.tools/files | 0.65 | CONNECTED | 1.0 | **0.65** |
| POST teams.gimme.tools/messages | 0.78 | KEYCHAIN | 0.8 | **0.62** |
| POST gsheets.gimme.tools/spreadsheets | 0.60 | AVAILABLE | 0.5 | **0.30** |

## UX Presentation

```
┌───────────────────────────────────────────────────────┐
│ "post the quarterly numbers to finance"               │
├───────────────────────────────────────────────────────┤
│                                                       │
│ ⭐ Slack - #finance channel         [Send Message]    │
│    POST slack.gimme.tools/messages                    │
│                                                       │
│ 📁 Slack - Upload file              [Upload]          │
│    POST slack.gimme.tools/files                       │
│                                                       │
│ 📁 Google Drive                     [Save]            │
│    POST gdrive.gimme.tools/files                      │
│                                                       │
│ 🔐 Microsoft Teams (in 1Password)   [Connect - FaceID]│
│    POST teams.gimme.tools/messages                    │
│                                                       │
└───────────────────────────────────────────────────────┘
```

## Edge Cases

### Ambiguous Intent

When semantic similarity is close across categories:

```
"Did you want to:"
- Send a message about the numbers?
- Upload a file containing the numbers?
- Share a spreadsheet?
```

### No Connected Matches

```
"We found tools that could help, but you'll need to connect first:"

🔐 Slack (in 1Password)    [Connect with FaceID]
🔗 Microsoft Teams         [Sign Up / Connect]
```

### No Matches

```
"I don't have a tool for that yet."

[Request Integration] → Opens integration request form
```

## Validation Checklist

When reviewing service discovery implementation:

- [ ] Intent extraction handles common verbs and objects
- [ ] Embeddings include canonical, predicates, objects, and examples
- [ ] Three-way join considers connected, keychain, and available status
- [ ] Status weights prioritize connected services appropriately
- [ ] Recency and preference boosts are applied correctly
- [ ] Ambiguous intents present clarification options
- [ ] Missing matches suggest connection options
- [ ] UX clearly indicates connection status
