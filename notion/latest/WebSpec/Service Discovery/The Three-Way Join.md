# The Three-Way Join

The Three-Way Join is the core discovery algorithm that matches user intent with available tools.

## The Three Data Sources

```
┌─────────────────────────────────────────────────────────────┐
│                       USER REQUEST                           │
│    "post the quarterly numbers to the finance channel"        │
└──────────────────────────────┬──────────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
┌──────────────────┐  ┌────────────────┐  ┌────────────────┐
│                  │  │                │  │                │
│  🔗 CONNECTED    │  │  📚 REGISTRY   │  │  🔐 KEYCHAIN    │
│     SERVICES     │  │                │  │     HINTS      │
│                  │  │  All available │  │                │
│  OAuth tokens    │  │  tools with    │  │  Domains user  │
│  already issued  │  │  embeddings    │  │  has creds for │
│                  │  │                │  │                │
└────────┬─────────┘  └───────┬────────┘  └───────┬────────┘
         │                      │                      │
         └─────────────┬───────┴──────────────┘
                       │
                       ▼
              ┌────────────────────┐
              │   THREE-WAY JOIN   │
              └────────────────────┘
```

---

## The Algorithm

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

---

## Result Categories

| Status | Description | UX | Weight |
| --- | --- | --- | --- |
| **CONNECTED** | OAuth already authorized | "Use this" button | 1.0 |
| **KEYCHAIN** | Found in password manager | "Connect with FaceID" | 0.8 |
| **AVAILABLE** | In registry, not connected | "Sign up / Connect" | 0.5 |
| **UNAVAILABLE** | Not in registry | Don't show | 0.0 |

---

## Scoring Formula

```
final_score = semantic_similarity * status_weight * recency_boost * preference_boost
```

Where:

| Factor | Range | Description |
| --- | --- | --- |
| semantic_similarity | 0.0 - 1.0 | Cosine similarity from vector search |
| status_weight | 0.0 - 1.0 | Based on connection status (see above) |
| recency_boost | 1.0 - 1.2 | Higher if user used this tool recently |
| preference_boost | 1.0 - 1.3 | Higher if user marked as preferred |

---

## Example Walkthrough

**Request:** "post the quarterly numbers to finance"

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
| --- | --- |
| POST slack.gimme.tools/messages | 0.85 |
| POST slack.gimme.tools/files | 0.82 |
| POST teams.microsoft.gimme.tools/messages | 0.78 |
| POST drive.google.gimme.tools/files | 0.65 |
| POST sheets.google.gimme.tools/spreadsheets | 0.60 |

### Step 3: User Classification

```yaml
User's connected services:
  - [slack.gimme.tools](http://slack.gimme.tools)
  - [gdrive.gimme.tools](http://gdrive.gimme.tools)

User's keychain hints:
  - [teams.microsoft.com](http://teams.microsoft.com)
  - [notion.so](http://notion.so)
```

| Tool | Similarity | Status | Weight | Final |
| --- | --- | --- | --- | --- |
| POST slack.gimme.tools/messages | 0.85 | CONNECTED | 1.0 | **0.85** |
| POST slack.gimme.tools/files | 0.82 | CONNECTED | 1.0 | **0.82** |
| POST teams.microsoft.gimme.tools/messages | 0.78 | KEYCHAIN | 0.8 | **0.62** |
| POST drive.google.gimme.tools/files | 0.65 | CONNECTED | 1.0 | **0.65** |
| POST sheets.google.gimme.tools/spreadsheets | 0.60 | AVAILABLE | 0.5 | **0.30** |

### Step 4: Ranked Results

```
1. ✅ POST slack.gimme.tools/messages     (0.85) - Connected
2. ✅ POST slack.gimme.tools/files        (0.82) - Connected  
3. ✅ POST drive.google.gimme.tools/files (0.65) - Connected
4. 🔐 POST teams.microsoft.gimme.tools/messages (0.62) - In keychain
5. 🔗 POST sheets.google.gimme.tools/spreadsheets (0.30) - Available
```

---

## UX Presentation

```
┌───────────────────────────────────────────────────────┐
│ "post the quarterly numbers to finance"              │
├───────────────────────────────────────────────────────┤
│                                                       │
│ ⭐ Slack - #finance channel         [Send Message]    │
│    POST slack.gimme.tools/messages?channel=finance    │
│                                                       │
│ 📁 Slack - Upload file              [Upload]          │
│    POST slack.gimme.tools/files?channel=finance       │
│                                                       │
│ 📁 Google Drive                      [Save]            │
│    POST drive.google.gimme.tools/files                │
│                                                       │
│ 🔐 Microsoft Teams (in 1Password)   [Connect - FaceID] │
│    POST teams.microsoft.gimme.tools/messages          │
│                                                       │
└───────────────────────────────────────────────────────┘
```

---

## Edge Cases

### No Connected Services Match

If only KEYCHAIN or AVAILABLE matches exist:

```
"We found tools that could help, but you'll need to connect first:"

🔐 Slack (in 1Password)    [Connect with FaceID]
🔗 Microsoft Teams          [Sign Up / Connect]
```

### No Matches At All

```
"I don't have a tool for that yet."

[Request Integration] → Opens integration request form
```

### Ambiguous Intent

When semantic similarity is similar across multiple categories:

```
"Did you want to:"

📝 Send a message about the numbers?
📁 Upload a file containing the numbers?
📊 Share a spreadsheet?
```