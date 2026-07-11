# Core Syntax

> **Status: Proposed (C).** This page is a human-readable overview of the full REST-hierarchy URL
> grammar — the multi-tenant vision, not the current gateway. The gateway that actually ships today
> (**Implemented (B)**) routes by subdomain + a literal path that **is** the MCP tool name (slashes
> fold to underscores) — there is no REST collection hierarchy, format suffix, or `id` segment
> enforced yet. See the [status matrix](../index.md#status) and [ROADMAP-C.md](../ROADMAP-C.md).

A quick overview of the WebSpec URL grammar. For the normative, formal specification see
[Complete Grammar (EBNF)](complete-grammar-ebnf.md); for provider-by-provider collection examples
see [Object Type System](object-type-system.md).

## URL Structure

```
{subdomain}.gimme.tools/{collection}/{id}/{collection}/{id}...{.format}
```

### Design Principle: No Redundancy

The subdomain IS the provider, so putting `.provider` in the path (e.g.
`slack.gimme.tools/message.slack`) is redundant — and **banned** by the grammar. The provider
appears exactly once, in the subdomain. See
[Complete Grammar (EBNF) → Invalid URLs](complete-grammar-ebnf.md#invalid-urls-old-syntax) for the
full rule and counter-examples.

## The Four Rules

1. **Subdomain = Provider** — identifies which service handles the request and forms an isolation
   boundary. This part *is* implemented today (Host-header routing).
2. **Path = REST Hierarchy** — nested `/collection/id/collection/id...` (**Proposed C**). Today the
   entire path is instead treated as a literal MCP tool name.
3. **Suffix = Format** — optional content-negotiation suffix such as `.pdf` or `.md` (**Proposed
   C**; not implemented).
4. **Query Params = Filtering/Pagination** — never hierarchy. Query strings are passed through as
   tool arguments today; the filtering/pagination *convention* described here is proposed.

### METHOD (Verb)

The HTTP method serves as both the semantic verb and the permission boundary. GET/POST/PUT/PATCH
routing and the definer-verb tamper check are **Implemented (B)**; DELETE and HEAD/OPTIONS
discovery semantics beyond a basic ping/schema are largely **Proposed (C)**. See
[Method Semantics](../http-methods/method-semantics.md).

| Method | Semantic Role |
|--------|---------------|
| GET | Read, retrieve, search, list |
| POST | Create, send, invoke, execute |
| PUT | Replace, overwrite |
| PATCH | Update, modify, append |
| DELETE | Remove, revoke, cancel |
| HEAD | Check existence, get metadata |
| OPTIONS | Discover capabilities |

## Gateway Behavior (as implemented today)

At the real gateway, routing is literal — no LLM interprets the path:

```
POST slack.gimme.tools/send_message
Content-Type: application/json

{"channel": "general", "text": "Hello team!"}
```

The gateway resolves the `Host` header to a service, then calls the MCP tool named by the path
(`send_message`), passing the JSON body as tool arguments.

The LLM-routed gateway sketched in earlier drafts (`POST api.gimme.tools/execute` with a free-text
`intent` field) is **Proposed (C)** — see [Service Discovery](../discovery/index.md).

## Examples

See [Complete Grammar (EBNF) → Examples](complete-grammar-ebnf.md#examples) for the full set of
proposed REST-hierarchy request examples, and
[Object Type System](object-type-system.md) for the standard collection tables per provider.
