# WebSpec

<aside>
🌐

**WebSpec** is a protocol specification for tool invocation that *meshes* with web architecture rather than building on top of it. Every layer of the protocol maps directly to an existing web primitive—DNS for discovery, subdomains for isolation, HTTP methods for verbs, paths for nouns, and browser security for capability boundaries.

</aside>

## Design Philosophy

- The Meshing Principle

    Most protocols treat the web as a dumb pipe—serializing custom schemas over HTTP as mere transport. WebSpec takes the opposite approach: **the web IS the protocol**.

    | Web Primitive | WebSpec Role |
    | --- | --- |
    | DNS | Service discovery |
    | Subdomain | Isolation boundary |
    | Path | Object type system |
    | HTTP Method | Verb + permission boundary |
    | Query params | Arguments |
    | TLS scope | Trust boundary |
    | Cookie scope | Auth boundary |
    | Same-origin policy | Capability boundary |
    | MIME types | Object type inheritance |

    `[TODO:IMAGE] Diagram contrasting "Building ON web" (custom protocol tunneled through HTTP) vs "Meshing WITH web" (protocol expressed through web primitives)`

- Why Surface Area Shrinks

    Every feature inherited from web architecture is:

    - Code we don't write
    - Bugs we don't create
    - CVEs we don't patch
    - Edge cases already handled by decades of battle-testing

    The browser's same-origin policy alone represents millions of engineering hours. We get it for the cost of *choosing subdomains correctly*.

- Why Flexibility Increases

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

---

## URL Grammar

[Core Syntax](WebSpec/URL%20Grammar/Core%20Syntax.md)

> Defines the URL structure: `{subdomain}.[gimme.tools/{collection}/{id}...{.format](http://gimme.tools/{collection}/{id}...{.format)}`. Subdomain = provider routing, path = REST hierarchy, suffix = format, query params = filtering. HTTP methods serve as both semantic verbs and permission boundaries.
>

[Object Type System](WebSpec/URL%20Grammar/Object%20Type%20System.md)

> Describes how WebSpec types map to MIME types and path hierarchies. Types can be explicit (`/files/12345.json`), inferred from context, or resolved through the type resolution chain. Supports inheritance (e.g., `image/png` → `image/*` → `*/*`) and collection patterns.
>

[Type Resolution & Inference](WebSpec/URL%20Grammar/Type%20Resolution%20&%20Inference.md)

> Explains how WebSpec infers missing URL components through a six-step resolution chain: explicit values take priority, followed by type hints (e.g., `.pdf` suggests document providers), provider hints (e.g., `.slack` defaults to message), payload analysis (MIME type inspection), user-configured preferences, and finally interactive disambiguation prompts. Includes detailed inference rules mapping types to providers, providers to default objects, and a resolution preview API for clients.
>

---

## HTTP Methods as Verbs

[Method Semantics](WebSpec/HTTP%20Methods%20as%20Verbs/Method%20Semantics.md)

> Explains how HTTP methods (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS) serve as semantic verbs rather than embedding verbs in paths. Maps natural language actions to HTTP methods with absorbed verb lists, and covers idempotency guarantees enabling safe retries and caching.
>

[Permission Scoping](WebSpec/HTTP%20Methods%20as%20Verbs/Permission%20Scoping.md)

> Describes how permissions become simple `METHOD:path` pattern matching. Defines scope grammar, token structure with audience claims, and shows how gateway enforcement reduces to trivial pattern matching. Includes scope hierarchy examples (read-only, write, admin) and user-facing authorization UI.
>

[HEAD & OPTIONS Discovery](WebSpec/HTTP%20Methods%20as%20Verbs/HEAD%20&%20OPTIONS%20Discovery.md)

> Details how HEAD enables lightweight probing (existence checks, metadata, permission validation) and OPTIONS enables capability discovery (service capabilities, object-specific params, resolution preview). Shows how WebSpec piggybacks on CORS pre-flight for self-documenting APIs.
>

[METHOD Tokenization (Prompt Injection Defense)](WebSpec/HTTP%20Methods%20as%20Verbs/METHOD%20Tokenization%20(Prompt%20Injection%20Defense).md)

> Structural defense against prompt injection by enforcing: exactly ONE raw METHOD per request (the HTTP method itself), zero untokenized METHODs in payloads. Tokenizes verb-like content in data positions, detokenizes only for human display—never for AI/agent contexts. Eliminates the verb/noun category confusion that enables injection attacks.
>

---

## Subdomain Architecture

[Isolation Model](WebSpec/Subdomain%20Architecture/Isolation%20Model.md)

> Uses subdomains as natural security boundaries, inheriting the browser's same-origin policy. Different subdomains = different origins = isolated by default. Defines special subdomains (`auth.`, `api.`, `local.`, `{service}.`) and maps attack vectors to their mitigations. Includes SSL/TLS wildcard considerations and optional Public Suffix List registration.
>

[Cookie & Token Scoping](WebSpec/Subdomain%20Architecture/Cookie%20&%20Token%20Scoping.md)

> Scopes cookies exclusively to the auth subdomain so services never see session tokens directly. Service tokens are audience-bound (via `aud` claim), short-lived, and method/path-scoped. Covers blast radius containment: a compromised service token only affects one service for a limited time.
>

[The [local.gimme.tools](http://local.gimme.tools) Bridge](WebSpec/Subdomain%20Architecture/The%20local%20gimme%20tools%20Bridge.md)

> Bridges to [`localhost:7001`](http://localhost:7001) via an outbound WebSocket tunnel, enabling local tool execution (file access, scripts, shell commands, app control, clipboard) with full WebSpec security. No firewall configuration required—the daemon initiates the connection. Device-bound tokens ensure stolen credentials fail on wrong machines. Optional local confirmation prompts for sensitive operations. Same auth model, permission scoping, and audit trail as cloud services.
>

[Local Service Architecture (Client-Side)](WebSpec/Subdomain%20Architecture/Local%20Service%20Architecture%20(Client-Side).md)

> **(Unix/macOS only)** Recommended client-side architecture that mirrors subdomain isolation using Unix sockets and OS-level sandboxing. Maps [`file.local.gimme.tools`](http://file.local.gimme.tools) to isolated socket services with entitlements (macOS) or systemd sandboxing (Linux). Implementation guidance, not protocol requirement.
>

---

## Authentication & Authorization

[Auth Flow Overview](WebSpec/Authentication%20&%20Authorization/Auth%20Flow%20Overview.md)

> Standard OAuth 2.0 with three authorization layers: platform ([gimme.tools](http://gimme.tools) as OAuth client), session (device-bound), and per-invocation confirmation. Covers service connection, session login, token requests, refresh, and revocation flows. Includes security checklist for cookies, tokens, PKCE, and state validation.
>

[Keychain Integration](WebSpec/Authentication%20&%20Authorization/Keychain%20Integration.md)

> Leverages password managers (1Password, Apple Keychain, Bitwarden) for zero-friction service discovery—your keychain already knows what services you use. Matches connected, available, and keychain-detected services via a three-way join. Biometric auth accelerates OAuth without ever exposing passwords to [gimme.tools](http://gimme.tools).
>

[Onboarding & Subdomain Provisioning](WebSpec/Authentication%20&%20Authorization/Onboarding%20&%20Subdomain%20Provisioning.md)

> Covers user onboarding flows (keychain detection, service connection prompts, guided OAuth), subdomain provisioning (wildcard DNS, TLS cert issuance, health checks), session management (device binding, refresh tokens, revocation), and multi-device coordination. Includes user-facing screens for service authorization and permission reviews.
>

[Token Structure](WebSpec/Authentication%20&%20Authorization/Token%20Structure.md)

> Defines JWT structure with standard claims (`iss`, `sub`, `aud`, `exp`) plus WebSpec claims (`scope`, `session_id`, `device_id`). Scope patterns use `METHOD:host/path` format with wildcards. Audience binding ensures tokens only work at their intended subdomain. Local bridge tokens add device binding for physical machine verification.
>

---

## Service Discovery

[NLP-Driven Resolution](WebSpec/Service%20Discovery/NLP-Driven%20Resolution.md)

> Resolves natural language requests to WebSpec routes via semantic understanding. Extracts predicates and objects from phrases like "fire off a quick note," normalizes them to canonical forms, generates embeddings, and ranks matches by semantic similarity, authorization status, user preference, and context hints. Configurable confirmation modes from always-confirm to auto-execute.
>

[Embedding Schema](WebSpec/Service%20Discovery/Embedding%20Schema.md)

> Defines how tools register for semantic discovery using vector embeddings. Tools specify canonical descriptions, predicate synonyms (e.g., "fire off," "shoot," "drop" → send), and object types. Multi-vector search across canonical, predicate, and object indices enables meaning-based matching. Includes similarity thresholds, caching strategy, and negative example "repulsion."
>

[The Three-Way Join](WebSpec/Service%20Discovery/The%20Three-Way%20Join.md)

> Core discovery algorithm that joins user intent against three data sources: connected services (OAuth tokens), the tool registry (all available tools with embeddings), and keychain hints (domains user has credentials for). Weighted scoring combines semantic similarity with connection status (Connected > Keychain > Available) plus recency and preference boosts.
>


---

## Service Registration

[Registration Schema](WebSpec/Service%20Registration/Registration%20Schema.md)

> Two-phase process: domain verification then tool registration. Services host a manifest at `/.well-known/gimme-tools.yaml` defining metadata, OAuth endpoints, canonical domains, and tool definitions with routes, semantics (predicates, objects, contexts), and parameters. Includes manifest validation rules, update flow, webhook notifications, and rate limits.
>

[Domain Verification](WebSpec/Service%20Registration/Domain%20Verification.md)

> Proves registrants control claimed domains via DNS TXT record, well-known file, or meta tag. Prevents subdomain squatting and OAuth hijacking. Subdomains auto-verify under a verified primary domain. Ongoing weekly/daily re-verification with graduated suspension policy (warning → flagged → suspended → deactivated) for failures.
>

---

## Reference

[Complete Grammar (EBNF)](WebSpec/Reference/Complete%20Grammar%20(EBNF).md)

> Formal grammar for WebSpec URLs: `https://{subdomain}.[gimme.tools/{path}{.format}?{query}](http://gimme.tools/{path}{.format}?{query})`. Subdomain = provider routing, path = REST hierarchy (`/collection/id/...`), format suffix = content negotiation (`.json`, `.md`, `.pdf`, etc.), query params = filtering only (never hierarchy). Includes regex patterns and validation examples.
>

[Comparison with MCP](WebSpec/Reference/Comparison%20with%20MCP.md)

> MCP optimizes for integration depth within specific apps (Claude Desktop, IDEs) using JSON-RPC over stdio/SSE. WebSpec optimizes for universal interoperability using standard HTTP/REST. MCP has lower "Hello World" barrier but requires SDK; WebSpec works with curl, browsers, and any HTTP client. Security: MCP relies on host app enforcement; WebSpec relies on browser same-origin policy and OS permissions.
>

---

## Conceptual Frameworks

Philosophical and semantic foundations that inform the protocol design. These sections are not normative but provide interpretive lenses for communication, documentation, and developer experience.

[Permissions as Trust Gradients (Philía Model)](WebSpec/Conceptual%20Frameworks/Permissions%20as%20Trust%20Gradients%20(Phil%C3%ADa%20Model).md)

Maps the Unix permission model to Greek concepts of love (philautia, philia, storge, eros, agape, pragma), revealing that permission structures encode ancient wisdom about trust relationships. Useful for UX framing, developer intuition, and explaining security postures to non-technical stakeholders.

Explores the structural isomorphism between Unix permission systems (owner/group/other × read/write/execute) and the Greek taxonomy of love (philautia, philia, agape). Reframes security as the discipline of healthy boundaries, with HTTP methods as relational verbs. Includes image generation prompts for visualizing the mappings.

Research:

[Monetization - Market Context & Positioning](WebSpec/Conceptual%20Frameworks/Monetization%20-%20Market%20Context%20&%20Positioning.md)
