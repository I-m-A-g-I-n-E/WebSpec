# NLP-Driven Resolution

Natural language requests are resolved to WebSpec routes through semantic understanding, not keyword matching.

## The Flow

```java
┌─────────────────────────────────────────────────────────────┐
│   "I need to fire off a quick note to Sarah about tomorrow"   │
└────────────────────────────┬────────────────────────────────┘
                                │
                       1. NLP Extraction
                                │
                                ▼
                    ┌───────────────────────┐
                    │ predicate: "send"     │
                    │ object: "message"     │
                    │ recipient: "Sarah"    │
                    │ topic: "tomorrow"     │
                    └───────────┬───────────┘
                                │
                       2. Embedding Generation
                                │
                                ▼
                    ┌───────────────────────┐
                    │ action_emb: [0.2, ...]  │
                    │ object_emb: [0.7, ...]  │
                    └───────────┬───────────┘
                                │
                       3. Semantic Search
                                │
                                ▼
                    ┌───────────────────────┐
                    │ Tool Registry          │
                    │ (vector database)      │
                    └───────────┬───────────┘
                                │
                       4. Ranking & Filtering
                                │
                                ▼
                    ┌───────────────────────┐
                    │ 1. POST /message.slack │
                    │ 2. POST /[message.email](http://message.email) │
                    │ 3. POST /message.sms   │
                    └───────────┬───────────┘
                                │
                       5. User Confirmation
                                │
                                ▼
                    ┌───────────────────────┐
                    │ "Send via Slack?"      │
                    │      [Confirm]         │
                    └───────────────────────┘
```

---

## NLP Extraction

The first step extracts semantic structure from natural language:

```json
// Input
"fire off a quick note to Sarah about the meeting"

// Extraction (via LLM or specialized NLP)
{
  "predicate": {
    "raw": "fire off",
    "canonical": "send",
    "confidence": 0.95
  },
  "object": {
    "raw": "note",
    "canonical": "message",
    "confidence": 0.90
  },
  "arguments": {
    "recipient": "Sarah",
    "topic": "the meeting"
  },
  "hints": {
    "urgency": "quick",
    "formality": "casual"
  }
}
```

### Predicate Normalization

| User Says | Canonical Predicate | HTTP Method |
| --- | --- | --- |
| fire off, shoot, drop | send | POST |
| check out, look at, pull up | read | GET |
| nuke, trash, get rid of | delete | DELETE |
| tweak, fix, update | modify | PATCH |

### Object Normalization

| User Says | Canonical Object |
| --- | --- |
| note, DM, text, ping | message |
| doc, paper, writeup | document |
| pic, photo, screenshot | image |
| spreadsheet, sheet, data | spreadsheet |

---

## Embedding Generation

Canonicalized predicates and objects are converted to embeddings:

```python
def generate_embeddings(extraction):
    # Combine predicate + object for action embedding
    action_text = f"{extraction['predicate']['canonical']} {extraction['object']['canonical']}"
    action_embedding = embed(action_text)  # e.g., "send message" -> [0.2, 0.8, ...]
    
    # Object embedding for type matching
    object_embedding = embed(extraction['object']['canonical'])
    
    return {
        "action": action_embedding,
        "object": object_embedding
    }
```

---

## Semantic Search

Embeddings are compared against the tool registry:

```python
def find_matching_tools(embeddings, user):
    # Search vector database
    candidates = vector_[db.search](http://db.search)(
        embedding=embeddings["action"],
        top_k=10,
        threshold=0.7
    )
    
    # Filter by user's authorized services
    authorized = [
        c for c in candidates 
        if c.service in user.connected_services
        or c.service in user.keychain_hints
    ]
    
    # Rank by:
    # 1. Semantic similarity
    # 2. User preference/history
    # 3. Authorization status
    return rank(authorized)
```

---

## Ranking Factors

| Factor | Weight | Description |
| --- | --- | --- |
| **Semantic similarity** | 40% | Embedding distance |
| **Authorization status** | 25% | Connected > Keychain > Available |
| **User preference** | 20% | Historical usage patterns |
| **Context hints** | 15% | Urgency, formality, recipient domain |

### Context Hints Example

```json
// "email [Sarah@company.com](mailto:Sarah@company.com) about the report"
{
  "hints": {
    "recipient_domain": "[company.com](http://company.com)",
    "suggests": "email"  // @domain syntax suggests email
  }
}

// "DM the team"
{
  "hints": {
    "recipient_type": "group",
    "suggests": ["slack", "teams", "discord"]
  }
}
```

---

## Resolution States

### Confident Match

```
Semantic similarity > 0.9
Single authorized service matches
→ "Send via Slack?" [Confirm] [Change]
```

### Multiple Matches

```
Semantic similarity > 0.7
Multiple authorized services match
→ Show ranked list, let user choose
```

### Low Confidence

```
Semantic similarity < 0.7
Or no authorized services
→ "I'm not sure what you want. Did you mean...?"
```

### No Match

```
No services in registry match
→ "I don't have a tool for that yet."
```

---

## Confirmation UX

Confirmation can be tuned per user:

| Mode | Behavior |
| --- | --- |
| **Always confirm** | Show proposed action, require click |
| **Confirm new** | Auto-execute familiar actions, confirm new ones |
| **Trust threshold** | Auto-execute if confidence > 0.95 |
| **Never confirm** | Execute immediately (power users) |

```yaml
user_preferences:
  confirmation_mode: "trust_threshold"
  trust_threshold: 0.92
  always_confirm_services: ["[local.gimme.tools](http://local.gimme.tools)"]  # Always confirm local
```