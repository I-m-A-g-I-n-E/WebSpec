# WebSpec

> **A protocol specification for tool invocation that *meshes* with web architecture rather than building on top of it.**

Every layer of the protocol maps directly to an existing web primitive — DNS for discovery, subdomains for isolation, HTTP methods for verbs, paths for nouns, and browser security for capability boundaries.

---

## The Meshing Principle

Most protocols treat the web as a dumb pipe — serializing custom schemas over HTTP as mere transport. WebSpec takes the opposite approach: **the web IS the protocol**.

| Web Primitive | WebSpec Role |
|---|---|
| DNS | Service discovery |
| Subdomain | Isolation boundary |
| Path | Object type system |
| HTTP Method | Verb + permission boundary |
| Query params | Arguments |
| TLS scope | Trust boundary |
| Cookie scope | Auth boundary |
| Same-origin policy | Capability boundary |
| MIME types | Object type inheritance |

## Why Surface Area Shrinks

Every feature inherited from web architecture is:

- Code we don't write
- Bugs we don't create
- CVEs we don't patch
- Edge cases already handled by decades of battle-testing

The browser's same-origin policy alone represents millions of engineering hours. We get it for the cost of *choosing subdomains correctly*.

## Why Flexibility Increases

Because WebSpec is isomorphic to HTTP itself:

```
Any HTTP client     → works
Any browser         → works
curl                → works
Postman             → works
Another AI agent    → works
A webhook           → works
A cron job          → works
```

No SDK required. No client library. No protocol translation.

## Specification Sections

- **[Design Philosophy](philosophy/index.md)** — Conceptual foundations, including the Philía trust model
- **[URL Grammar](url-grammar/index.md)** — Core syntax, type system, resolution, and formal EBNF grammar
- **[HTTP Methods](http-methods/index.md)** — Method semantics, permission scoping, discovery, tokenization, and definer verbs
- **[Subdomain Architecture](subdomain-architecture/index.md)** — Isolation model, cookie scoping, local bridge, and client-side architecture
- **[Authentication](auth/index.md)** — OAuth flows, keychain integration, onboarding, and token structure
- **[Service Discovery](discovery/index.md)** — NLP-driven resolution, embedding schema, and the three-way join
- **[Service Registration](registration/index.md)** — Registration schema and domain verification
- **[Reference](reference/index.md)** — Comparisons and appendices

## Status

| Section | Status |
|---|---|
| URL Grammar | Draft |
| HTTP Methods | Draft |
| Subdomain Architecture | Draft |
| Authentication | Draft |
| Service Discovery | Draft |
| Service Registration | Draft |
| Definer Verbs & Payload Binding | Draft (new) |
| Philosophical Foundations | Conceptual (non-normative) |
