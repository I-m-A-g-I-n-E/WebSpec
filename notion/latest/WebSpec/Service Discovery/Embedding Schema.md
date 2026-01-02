# Embedding Schema

The embedding schema defines how tools are registered for semantic discovery.

## Purpose

Instead of requiring exact keyword matches, WebSpec uses vector embeddings to find tools based on **meaning**. This allows:

- "fire off a note" to match `POST /message`
- "nuke this file" to match `DELETE /file`
- "grab the quarterly numbers" to match `GET /spreadsheet`

---

## Tool Registration Record

```yaml
tool:
  id: "slack-send-message"
  service: "[slack.gimme.tools](http://slack.gimme.tools)"
  
  # The WebSpec route
  route:
    method: POST
    path: /messages
    types: [text, html, blocks]
  
  # Semantic anchors for embedding generation
  semantics:
    canonical: "send message via Slack"
    predicates:
      - send
      - post
      - message
      - notify
      - ping
      - DM
    objects:
      - message
      - note
      - notification
      - text
    contexts:
      - team communication
      - workplace messaging
      - instant messaging
    urgency: [medium, high]
    formality: [casual]
  
  # Pre-computed embeddings (generated at registration)
  embeddings:
    canonical: [0.234, 0.891, ...]      # 1536-dim vector
    predicate_centroid: [0.412, ...]    # Average of predicate embeddings
    object_centroid: [0.667, ...]       # Average of object embeddings
  
  # Required parameters
  params:
    channel:
      type: string
      required: true
      description: "Slack channel or user to message"
    body:
      type: string
      required: true
      description: "Message content"
    thread:
      type: string
      required: false
      description: "Thread ID for replies"
```

---

## Embedding Generation

### At Registration Time

When a service registers a tool, the server generates embeddings:

```python
def register_tool(tool_spec):
    # Generate canonical embedding
    canonical_emb = embed(tool_spec['semantics']['canonical'])
    
    # Generate predicate centroid
    pred_embeddings = [embed(p) for p in tool_spec['semantics']['predicates']]
    pred_centroid = average(pred_embeddings)
    
    # Generate object centroid
    obj_embeddings = [embed(o) for o in tool_spec['semantics']['objects']]
    obj_centroid = average(obj_embeddings)
    
    # Store in vector database
    vector_db.upsert(
        id=tool_spec['id'],
        vectors={
            'canonical': canonical_emb,
            'predicate': pred_centroid,
            'object': obj_centroid
        },
        metadata=tool_spec
    )
```

### At Query Time

User requests are embedded and compared:

```python
def find_tools(user_request):
    # Extract and embed user intent
    extraction = nlp_extract(user_request)
    query_emb = embed(f"{extraction['predicate']} {extraction['object']}")
    
    # Multi-vector search
    results = vector_[db.search](http://db.search)(
        queries=[
            ('canonical', query_emb, weight=0.5),
            ('predicate', embed(extraction['predicate']), weight=0.3),
            ('object', embed(extraction['object']), weight=0.2)
        ],
        top_k=10
    )
    
    return results
```

---

## Embedding Model

Recommended: OpenAI `text-embedding-3-small` or similar

| Model | Dimensions | Cost | Quality |
| --- | --- | --- | --- |
| text-embedding-3-small | 1536 | Low | Good |
| text-embedding-3-large | 3072 | Medium | Better |
| Cohere embed-v3 | 1024 | Medium | Good |
| Local (e5-base) | 768 | Free | Acceptable |

---

## Similarity Metrics

### Cosine Similarity

```python
def cosine_similarity(a, b):
    return dot(a, b) / (norm(a) * norm(b))
```

### Thresholds

| Similarity | Interpretation |
| --- | --- |
| > 0.90 | Strong match, high confidence |
| 0.80 - 0.90 | Good match, confirm with user |
| 0.70 - 0.80 | Possible match, show alternatives |
| < 0.70 | Weak match, ask for clarification |

---

## Index Structure

```yaml
vector_indices:
  # Primary: canonical description
  canonical_index:
    dimensions: 1536
    metric: cosine
    
  # Secondary: action verbs
  predicate_index:
    dimensions: 1536
    metric: cosine
    
  # Tertiary: object types
  object_index:
    dimensions: 1536
    metric: cosine

metadata_indices:
  # For filtering
  service_id: keyword
  method: keyword
  object_type: keyword
  connected_users: keyword[]  # For fast filtering by user
```

---

## Query Expansion

To improve recall, queries can be expanded:

```python
def expand_query(predicate, object):
    # Use LLM to generate synonyms
    expansions = llm.complete(f"""
        Given the action "{predicate} {object}", 
        list 5 synonymous phrases:
    """)
    
    # Embed all expansions
    embeddings = [embed(e) for e in expansions]
    
    # Return centroid for robust matching
    return average(embeddings)
```

---

## Negative Examples

Tools can register what they're NOT:

```yaml
tool:
  id: "slack-send-message"
  semantics:
    canonical: "send message via Slack"
    not:
      - "send email"      # Prevent confusion with email
      - "schedule message" # Different tool
      - "send file"       # Use /files endpoint instead
```

Negative examples create "repulsion" in the embedding space.

---

## Caching Strategy

```yaml
cache_layers:
  # Hot: Recent user queries
  l1_cache:
    type: in-memory
    ttl: 5m
    key: hash(user_id + normalized_query)
    
  # Warm: Common queries
  l2_cache:
    type: redis
    ttl: 1h
    key: hash(normalized_query)
    
  # Cold: Full vector search
  l3_search:
    type: vector_db
    index: all_tools
```

`[TODO:DIAGRAM] Visual representation of multi-vector search across canonical, predicate, and object indices`