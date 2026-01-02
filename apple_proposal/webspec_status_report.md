# WebSpec Protocol Specification - Status Report

**Generated:** 2025-12-16
**Source:** https://www.notion.so/WebSpec-2c942c9038be80c2b26ee86a5ea677c5

---

## Executive Summary

| Metric | Count |
|--------|-------|
| Total Pages | 24 |
| Populated Pages | 20 |
| Empty Pages | 4 |
| Completion Rate | **83%** |
| TODOs Found | 7 |

The WebSpec specification is substantially documented with strong coverage of core protocol mechanics. The main gaps are in the Reference section (EBNF grammar, examples, MCP comparison) and one introductory placeholder page.

---

## Page Status Overview

### ✅ Populated (20 pages)
### ❌ Empty (4 pages)

---

## Detailed Page Assessment

### Main Page: WebSpec
**Status:** ✅ POPULATED
**Summary:** Introduces WebSpec as a web-native tool invocation protocol. Establishes the "Meshing Principle" - using web primitives (DNS, subdomains, HTTP methods, paths) as the protocol itself rather than building on top of HTTP. Includes comparison table mapping web primitives to WebSpec roles.

**TODO Found:** `[TODO:IMAGE]` Diagram contrasting "Building ON web" vs "Meshing WITH web"

---

## URL Grammar Section (4 pages)

### 1. WebSpec URLs Introduction
**Status:** ❌ EMPTY
**Notes:** This is a placeholder/intro page with the section description in the title but no body content.

### 2. Core Syntax
**Status:** ✅ POPULATED
**Summary:** Defines the complete URL grammar including METHOD (verb), service (subdomain), object (noun), type (format/subtype), provider (destination), id (instance), and params (arguments). Includes visual grammar diagram, component tables, and formal grammar skeleton.

### 3. Object Type System
**Status:** ✅ POPULATED
**Summary:** Unifies tool targets with file types into a single abstraction. Documents standard object categories (communication, content, productivity, data) with provider mappings. Covers type qualifiers, format types, provider types, vendor extensions (x-prefix), type hierarchy, and conversion operations.

**TODO Found:** `[TODO:DIAGRAM]` Visual hierarchy tree showing object inheritance relationships

### 4. Type Resolution & Inference
**Status:** ✅ POPULATED
**Summary:** Six-step resolution chain: EXPLICIT → TYPE_HINT → PROVIDER_HINT → PAYLOAD → USER_DEFAULT → PROMPT. Documents inference rules for type→provider, provider→object, and payload→object+type mappings. Includes user preferences schema and disambiguation prompt UX.

---

## HTTP Methods as Verbs Section (3 pages)

### 5. Method Semantics
**Status:** ✅ POPULATED
**Summary:** Restores HTTP methods' semantic richness. Maps GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS to their natural language equivalents with "absorbed verbs" lists. Documents idempotency guarantees and explains why this matters for permissions.

### 6. Permission Scoping
**Status:** ✅ POPULATED
**Summary:** Permissions as `METHOD:host/path` patterns. Includes scope grammar, example patterns, token structure with `aud` claim, gateway enforcement code (6 lines of Python), enforcement examples table, scope hierarchies (read-only, write, admin), and scope request UI mockup.

### 7. HEAD & OPTIONS Discovery
**Status:** ✅ POPULATED
**Summary:** HEAD for lightweight probing (existence, metadata, permissions). OPTIONS for capability discovery. Includes response examples, CORS pre-flight integration, and self-documenting API concepts.

**TODO Found:** `[TODO:IMAGE]` Diagram showing OPTIONS request/response flow with capability discovery

---

## Subdomain Architecture Section (3 pages)

### 8. Isolation Model
**Status:** ✅ POPULATED
**Summary:** Leverages browser's same-origin policy for security. Documents special subdomains (auth, api, local, service), what services CANNOT do, SSL/TLS wildcard considerations, and trust boundaries diagram. Mentions Public Suffix List option for maximum isolation.

### 9. Cookie & Token Scoping
**Status:** ✅ POPULATED
**Summary:** Cookies scoped to auth subdomain only (HttpOnly, Secure, SameSite=Strict). Token flow from session token through service token issuance. Audience binding prevents token leakage. Blast radius containment table comparing compromises with/without scoping.

### 10. Local Service Architecture (Client-Side)
**Status:** ✅ POPULATED
**Summary:** Unix/macOS implementation guide for local service isolation using Unix sockets and OS-level sandboxing. Maps web subdomain isolation to socket paths. Covers XPC services (macOS), systemd sandboxing (Linux), service isolation matrix, and security properties.

**TODO Found:** `[TODO:IMPLEMENTATION]` Architecture is recommended but not required for compliance

---

## Authentication & Authorization Section (3 pages)

### 11. Auth Flow Overview
**Status:** ✅ POPULATED
**Summary:** Standard OAuth 2.0 with platform-level authorization. Three authorization layers: Platform, Session, Invocation. Documents service connection flow, session authentication, complete request flow sequence diagram, token refresh, and revocation mechanisms. Includes security checklist.

### 12. Keychain Integration
**Status:** ✅ POPULATED
**Summary:** Leverages password managers for zero-friction service discovery. Documents the three-way join (connected services × registry × keychain), match priority UX, supported providers (1Password, Apple Keychain, Bitwarden), domain matching logic, and biometric auth flow.

### 13. Token Structure
**Status:** ✅ POPULATED
**Summary:** JWT structure with WebSpec-specific claims. Three token types: Session, Service, Refresh. Documents standard claims (iss, sub, aud, iat, exp, jti) and WebSpec claims (scope, session_id, device_id). Includes scope pattern syntax, validation algorithm, and local bridge token specifics.

---

## Service Discovery Section (3 pages)

### 14. NLP-Driven Resolution
**Status:** ✅ POPULATED
**Summary:** Semantic understanding for natural language → WebSpec route mapping. Documents NLP extraction (predicate/object normalization), embedding generation, semantic search against registry, ranking factors (40% similarity, 25% auth status, 20% user preference, 15% context), and resolution states.

### 15. Embedding Schema
**Status:** ✅ POPULATED
**Summary:** Vector embedding structure for tool registration. Documents tool registration records with semantic fields (canonical, predicates, objects, contexts, not), embedding generation at registration and query time, embedding model recommendations, similarity thresholds, index structure, and caching strategy.

**TODO Found:** `[TODO:DIAGRAM]` Visual representation of multi-vector search across canonical, predicate, and object indices

### 16. The Three-Way Join
**Status:** ✅ POPULATED
**Summary:** Core discovery algorithm matching user intent with available tools. Three data sources: CONNECTED (OAuth), REGISTRY (all tools), KEYCHAIN (credentials). Documents the algorithm, result categories (CONNECTED/KEYCHAIN/AVAILABLE/UNAVAILABLE), scoring formula, example walkthrough, and UX presentation.

---

## Service Registration Section (2 pages)

### 17. Registration Schema
**Status:** ✅ POPULATED
**Summary:** Two-phase process: domain verification + tool registration. Documents service manifest location (`/.well-known/gimme-tools.yaml`), full manifest schema (service metadata, OAuth config, tools array), registration API, manifest validation checks, tool schema details, update flow, webhook notifications, and rate limits.

### 18. Domain Verification
**Status:** ✅ POPULATED
**Summary:** Three verification methods: DNS TXT record (recommended), well-known file, meta tag. Documents verification flow, multi-domain rules, token security, ongoing verification schedule, and failure handling timeline (warning → suspension → deactivation).

---

## Reference Section (3 pages)

### 19. Complete Grammar (EBNF)
**Status:** ❌ EMPTY
**Notes:** Full formal grammar specification not yet written.

### 20. Examples & Use Cases
**Status:** ❌ EMPTY
**Notes:** Practical examples and use cases not yet documented.

### 21. Comparison with MCP
**Status:** ❌ EMPTY
**Notes:** Comparison with Anthropic's Model Context Protocol not yet written.

---

## Conceptual Frameworks Section (3 pages)

### 22. Permissions as Trust Gradients (Philía Model)
**Status:** ✅ POPULATED
**Summary:** Maps Unix permissions to Greek love concepts (philautia, philia, agape). Owner/Group/Other as trust circles. Read/Write/Execute as vulnerability types. Documents extended mappings, special bits as relational modifiers, pathological cases (narcissism, codependency, 777), and applications for UX, security education, and marketing.

**TODOs Found:**
- `[TODO:RESEARCH]` Investigate extension to capability-based security, RBAC, zero-trust
- `[TODO:UX]` Prototype permission prompts with Philía language
- `[TODO:WRITING]` Develop as standalone essay

### 23. Philosophical Foundations: Permissions as Love Grammar
**Status:** ✅ POPULATED
**Summary:** Extended exploration of permission/love isomorphism. HTTP methods as relational verbs (GET=Eros, HEAD=Paternal Storge, POST=Generative Storge, PATCH=Nurturing Storge, PUT=Pragma, DELETE=Release, OPTIONS=Agape). Directories as maternal containment. Security as healthy boundaries. Includes detailed image generation prompts for visualizations.

### 24. The local.gimme.tools Bridge
**Status:** ✅ POPULATED
**Summary:** Bridges cloud to localhost:7001 for local tool execution. Documents architecture (outbound WebSocket tunnel), connection flow, security model (device binding, local confirmation), and available local tools (file, code, shell, app, clipboard).

---

## Summary: What IS Documented

### Core Protocol (Fully Documented)
- URL grammar and syntax
- Object type system with inheritance
- Type resolution and inference chain
- HTTP method semantics
- Permission scoping with pattern matching
- HEAD/OPTIONS discovery mechanisms

### Security Architecture (Fully Documented)
- Subdomain isolation model
- Cookie and token scoping
- Session/service/refresh token structure
- JWT claims and validation
- Blast radius containment

### Authentication (Fully Documented)
- OAuth 2.0 integration
- Three-tier authorization (platform, session, invocation)
- Keychain integration for discovery
- Token lifecycle and revocation

### Service Discovery (Fully Documented)
- NLP-driven resolution pipeline
- Embedding schema for tools
- Three-way join algorithm
- Ranking and scoring

### Service Registration (Fully Documented)
- Manifest schema and format
- Domain verification methods
- Webhook notifications
- Rate limits

### Conceptual Foundations (Documented)
- Permission/love isomorphism
- Philosophical grounding
- Local bridge architecture

---

## Summary: What's MISSING or Empty

### Empty Pages (Need Content)
1. **Complete Grammar (EBNF)** - Formal specification needed
2. **Examples & Use Cases** - Practical examples needed
3. **Comparison with MCP** - Competitive analysis needed
4. **WebSpec URLs Introduction** - Section intro placeholder

### Incomplete Elements (TODOs)
1. Visual diagram: "Building ON web" vs "Meshing WITH web"
2. Visual diagram: Object type inheritance hierarchy
3. Visual diagram: OPTIONS request/response flow
4. Visual diagram: Multi-vector search
5. Research: Extension to capability-based security models
6. UX prototype: Permission prompts with Philía language
7. Writing: Standalone essay on permission/love isomorphism

---

## Recommendations

### High Priority
1. **Write Complete Grammar (EBNF)** - Critical for implementers
2. **Create Examples & Use Cases** - Essential for adoption
3. **Write MCP Comparison** - Important for positioning

### Medium Priority
4. Create the missing visual diagrams
5. Fill in the URL Grammar introduction page

### Low Priority (Nice to Have)
6. Philía model research extensions
7. UX prototypes for permission prompts
8. Standalone essay development

---

## Conclusion

WebSpec is **substantially documented** with 83% of pages containing meaningful content. The core protocol specification is complete and detailed. The main gaps are in the Reference section, which would benefit from formal grammar, practical examples, and competitive comparison. The conceptual frameworks are unusually rich for a protocol spec, providing philosophical grounding that could aid developer relations and user education.

