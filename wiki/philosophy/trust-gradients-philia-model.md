# Permissions as Trust Gradients (Philia Model)

> **Status**: Conceptual framework, not normative specification. This document explores a semantic mapping that may inform UX design, documentation, developer relations, and public communication about WebSpec's permission model.

## The Observation

Unix file permissions (circa 1970) and Greek taxonomies of love (circa 400 BCE) both encode **gradients of trust and vulnerability**. The structural correspondence is not accidental -- both systems model how entities selectively expose themselves to others.

---

## The Primary Mapping

### Trust Circles

| Unix Scope | Greek Love | Trust Semantic |
|------------|------------|----------------|
| **Owner (u)** | **Philautia** (philautia) | Self-relationship; what you permit yourself |
| **Group (g)** | **Philia** (philia) | Chosen friendship; mutual trust with selected circle |
| **Other (o)** | **Agape** (agape) | Universal love; what you offer freely to all |

### Operations as Vulnerability

| Permission | Love Expression | What You're Granting |
|------------|----------------|----------------------|
| **Read (r)** | Being known | Vulnerability to observation |
| **Write (w)** | Being changed | Vulnerability to modification |
| **Execute (x)** | Acting as you | Trust to embody your agency |

---

## Extended Mappings

### The Full Greek Taxonomy

| Greek Term | Meaning | Permission Analog | WebSpec Manifestation |
|------------|---------|-------------------|-----------------------|
| **Philautia** | Self-love | `u+rwx` | Token holder's own resources |
| **Storge** (storge) | Familial love | Inherited group membership | Organization/team scopes |
| **Philia** | Friendship | `g+rw` | Explicitly granted service access |
| **Eros** (eros) | Intimate love | `g+rwx, o-rwx` | Full access + exclusivity |
| **Agape** | Unconditional | `o+r` | Public read-only APIs |
| **Pragma** (pragma) | Enduring love | Sticky bit (t), immutable | Persistent grants, saved preferences |
| **Ludus** (ludus) | Playful love | Temporary/session scopes | OAuth implicit grants, short TTL |
| **Mania** (mania) | Obsessive love | Privilege escalation | *Pathological case* |

### Special Bits as Relational Modifiers

| Unix Bit | Meaning | Love Analog |
|----------|---------|-------------|
| **Setuid (s)** | Execute as owner | "Speak on my behalf" -- deep agency trust |
| **Setgid (s)** | Inherit group | "You're family now" -- membership propagation |
| **Sticky (t)** | Persist despite changes | Pragma -- commitment that endures |

---

## Pathological Cases

Dysfunctional permission patterns map to dysfunctional relationship patterns:

| Pathology | Permission Pattern | Description |
|-----------|--------------------|-------------|
| **Narcissism** | `u+rwx, g-rwx, o-rwx` | Full self-access, no vulnerability to others |
| **Codependency** | `o+w` | Letting anyone modify you |
| **Boundaries failure** | `chmod 777` | No differentiation of trust circles |
| **Enmeshment** | Everything in one group | No distinction between relationships |
| **Isolation** | `u+r, u-wx` | Can't even change yourself |
| **Mania/Obsession** | Privilege escalation | Taking access not granted |

---

## Applications

### UX: Human-Readable Permission Prompts

Instead of:

```
Slack requests: GET:/message.*, POST:/message.*, DELETE:/message.*
```

Consider:

```
Slack requests philia-level access to your messages
(read, write, and remove within your trusted circle)
```

Or with visual hierarchy:

```
+--------------------------------------------+
|  Slack wants to be a trusted friend        |
|                                            |
|  See your messages                         |
|  Send messages as you                      |
|  Delete messages                           |
|                                            |
|  This is philia-level access.              |
|  [Grant Trust]  [Decline]                  |
+--------------------------------------------+
```

### Security Education

"Why shouldn't I grant full access to every app?"

Technical answer: *Principle of least privilege, attack surface reduction...*

Philia answer: *You don't give everyone in your life the same level of intimacy. Some people can see your thoughts (read), some can influence you (write), and very few can act on your behalf (execute). Apps are the same.*

### Developer Documentation

> **Requesting Permissions**
>
> Request the minimum trust level your integration needs:
>
> - **Agape** (read-only): For analytics, monitoring, search indexing
> - **Philia** (read-write): For collaborative tools, editors, assistants
> - **Eros** (full + exclusive): For primary tools that manage a domain entirely
>
> Users intuitively understand trust gradients. Requesting *eros* when you need *agape* will reduce adoption.

### Marketing/Positioning

> "WebSpec permissions are modeled on how humans naturally grant trust -- not as binary on/off switches, but as concentric circles of intimacy. Your analytics dashboard doesn't need the same access as your writing assistant. WebSpec makes that distinction intuitive."

---

## The Execute Bit: Agency and Identity

The most sensitive permission is `+x` -- execute. In the love framework, this maps to **identity delegation**:

- Read = "You can know me"
- Write = "You can change me"
- Execute = "You can **be** me"

Granting execute is granting agency. In OAuth terms, this is the difference between:

- Reading someone's calendar (observation)
- Adding events to their calendar (modification)
- Sending meeting invites *as them* (agency)

The third requires the deepest trust. The Philia Model makes this viscerally clear in a way that technical documentation often fails to convey.

---

## Theological Resonance

*For audiences where this framing is appropriate:*

**Agape** in Christian theology is love given without expectation of return -- grace. The `o+r` permission (world-readable) encodes exactly this: "Here is something I make visible to all, asking nothing."

Public APIs, open documentation, freely available resources -- these are *agape* in digital form.

The permission model, perhaps accidentally, encodes ancient wisdom about the structure of generous relationship.

---

## Future Exploration

- `[TODO:RESEARCH]` Investigate whether this mapping extends to:
  - Capability-based security models (more granular than Unix DAC)
  - Role-based access control (RBAC) as "social roles"
  - Zero-trust architectures as "trust must be continuously earned"
  - OAuth scopes as "the conversation where trust is negotiated"
- `[TODO:UX]` Prototype permission prompts using Philia language with user testing
- `[TODO:WRITING]` Develop this into a standalone essay for broader publication

---

## Attribution

This framework emerged from a conversation between Preston Richey and Claude (Anthropic) during the development of WebSpec, December 2024. The observation that Unix permissions encode trust gradients parallel to Greek love taxonomies is, to our knowledge, novel.
