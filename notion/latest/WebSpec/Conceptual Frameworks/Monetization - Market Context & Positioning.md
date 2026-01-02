# Monetization - Market Context & Positioning

<aside>
🎯

This page analyzes the competitive landscape for AI tool protocols and API aggregation services. It identifies gaps that WebSpec addresses and provides linkable reference content for implementations like [**gimme.tools**](http://gimme.tools).

</aside>

## The Problem Space

AI agents need to invoke tools across multiple providers. The current landscape fragments into four distinct categories, each solving part of the problem:

| Category | What They Do | What They Don't Do |
| --- | --- | --- |
| **Unified API Platforms** | Normalize disparate APIs into unified schemas per category | Security isolation, AI-native discovery, user-facing auth |
| **API Gateways** | Routing, rate limiting, auth, observability | Cross-provider normalization, semantic discovery |
| **Identity Providers** | User authentication, SSO, token management | API aggregation, tool execution, AI-specific patterns |
| **MCP Ecosystem** | Standardized tool protocol for AI agents | Managed hosting, security isolation, auth aggregation |

---

## Category 1: Unified API Platforms

These services aggregate APIs within vertical categories (CRM, HR, Accounting) and present normalized data models.

| Provider | Focus | Differentiator |
| --- | --- | --- |
| Apideck | Broad categories (CRM, HR, Accounting) | 200+ connectors, normalized data models |
| Merge | HR, ATS, Payroll, Accounting | Deep category focus, customer success tooling |
| Finch | HR/Payroll | Employment data specialist |
| Plaid / Tink | Banking/Fintech | Financial data aggregation |
| Nylas | Email/Calendar | Communications-focused |
| Duffel | Travel/Airlines | Vertical-specific booking APIs |
| DigitalAPI | Enterprise API management | Gateway-agnostic, AI-powered discovery |
- Gap Analysis
    
    Unified APIs solve the **normalization** problem but not the **security** problem. They:
    
    - Aggregate on the *server side* (your credentials live with them)
    - Don't provide subdomain isolation
    - Don't support AI-native discovery patterns
    - Don't distinguish between provider identity and service capability

---

## Category 2: API Gateways

Infrastructure for routing, observability, and policy enforcement at the API layer.

| Provider | Focus | Notable Features |
| --- | --- | --- |
| Kong Konnect | Multi-cloud, unified platform | MCP support, LLM routing, developer portal |
| AWS API Gateway | AWS ecosystem | Lambda integration, IAM-native |
| Cloudflare | Edge deployment | MCP server hosting, Workers integration |
| Apigee (Google) | Enterprise API management | Analytics, monetization |
- Gap Analysis
    
    Gateways solve **routing and policy** but not **semantic discovery**:
    
    - No understanding of what tools *do*, only where they *are*
    - No cross-provider normalization
    - Enterprise-focused, not consumer/prosumer
    - MCP support is additive, not native

---

## Category 3: Identity Providers

Authentication and authorization infrastructure for applications.

| Provider | Focus | Notable Features |
| --- | --- | --- |
| Auth0 | B2B SaaS auth | Organizations, SSO federation, 2M+ orgs/tenant |
| Okta | Enterprise SSO | FGA (fine-grained authorization), workforce identity |
| WorkOS | B2B auth primitives | SSO, SCIM, MFA, AuthKit |
| Authlete | OAuth/OIDC as API | Build-your-own flows, protocol compliance |
- Gap Analysis
    
    Identity providers handle auth **TO your app** but not **FROM your app TO third parties**:
    
    - User authenticates to your application (inbound)
    - No aggregation of user's existing OAuth tokens (outbound)
    - No tool execution layer
    - No AI-specific patterns (discovery, streaming, encryption)

---

## Category 4: MCP Ecosystem

The Model Context Protocol, standardizing AI-to-tool communication.

| Player | Role | Status |
| --- | --- | --- |
| Anthropic | Protocol specification + reference servers | Donated to Linux Foundation (Dec 2025) |
| OpenAI | MCP adoption in Agents SDK | Responses API, hosted MCP |
| mcp-agent | Agent framework with MCP aggregation | Open source, Temporal backend |
| Cloudflare | MCP server hosting | Edge deployment, Workers |
| Microsoft Copilot Studio | MCP connector integration | Enterprise governance controls |
- Gap Analysis
    
    MCP defines the **protocol** but not the **infrastructure**:
    
    - No managed hosting solution
    - Security is implementation-dependent
    - No subdomain isolation model
    - No auth aggregation (each server handles its own)
    - Transport-agnostic (STDIO, SSE) but not web-native

---

## The WebSpec Synthesis

WebSpec addresses gaps across all four categories by treating **the web itself as the protocol**.

| Capability | Unified APIs | Gateways | IdPs | MCP | WebSpec |
| --- | --- | --- | --- | --- | --- |
| Cross-provider normalization | ✅ | ❌ | ❌ | Partial | ✅ |
| Subdomain security isolation | ❌ | Partial | ❌ | ❌ | ✅ |
| OAuth aggregation (outbound) | ❌ | ❌ | ❌ | ❌ | ✅ |
| AI-native discovery (HEAD/OPTIONS) | ❌ | ❌ | ❌ | ✅ | ✅ |
| Bioauth integration | ❌ | ❌ | Partial | ❌ | ✅ |
| Provider/Service distinction | ❌ | ❌ | ❌ | ❌ | ✅ |
| Encrypted response channel | ❌ | ❌ | ❌ | ❌ | ✅ |
| No SDK required | ❌ | ❌ | ❌ | ❌ | ✅ |

---

## Unique Positioning

- Security as the Primary Feature
    
    Anyone can aggregate APIs. Nobody is treating the **security of AI tool execution** as the primary feature. WebSpec makes security architectural:
    
    - Subdomain isolation leverages browser same-origin policy
    - Cookie scoping prevents cross-provider token leakage
    - Encrypted responses protect data from the transport layer
    - Bioauth gates OAuth flows with hardware attestation
- Web-Native Design
    
    Most protocols treat HTTP as a dumb pipe. WebSpec treats **the web as the protocol**:
    
    - DNS for discovery
    - Subdomains for isolation boundaries
    - HTTP methods for verbs AND permission boundaries
    - HEAD/OPTIONS for AI-native capability discovery
    - MIME types for object type inheritance
- Provider vs Service Distinction
    
    No existing system cleanly separates:
    
    - **Provider**: The company/platform (Slack, Google, GitHub)
    - **Service**: The capability offered (messaging, storage, code hosting)
    
    This enables queries like:
    
    - "Send a message" (service) → resolves to user's configured provider
    - "Use Slack" (provider) → uses that specific provider
    - "Send via Slack messaging" (both) → explicit full path

---

## Implementation Reference

The following sections define capabilities that implementations (like [gimme.tools](http://gimme.tools)) can reference:

### 🔐 Subdomain Isolation Model

Each provider operates on its own subdomain (e.g., [`slack.gimme.tools`](http://slack.gimme.tools), [`gdrive.gimme.tools`](http://gdrive.gimme.tools)). This leverages browser same-origin policy to prevent:

- Cross-provider cookie access
- Cross-provider localStorage access
- Cross-provider DOM manipulation
- Cross-provider network request visibility

Tokens scoped to one subdomain cannot be read by another subdomain, even within the same root domain.

### 📜 OAuth Aggregation Pattern

WebSpec aggregates OAuth tokens **on behalf of the user**, not on the server:

- User authenticates with each provider individually
- Tokens are stored in user's keychain (not server database)
- Gateway retrieves tokens via secure channel at execution time
- Tokens never persist in gateway memory beyond request lifecycle

### 🔍 AI-Native Discovery Protocol

**HEAD** returns lightweight property listing:

```
HEAD /channels/C123/messages/M456

X-Properties-Default: id, text, user, ts
X-Properties-Optional: attachments, reactions
X-Properties-Calculated: sentiment, word_count
X-Content-Formats: json, md, txt
```

**OPTIONS** returns full schema with descriptions, costs, and method-specific capabilities.

### 🔒 Encrypted Response Channel

Responses can be encrypted end-to-end:

- Client generates per-thread encryption key
- Key is transmitted via secure side-channel (not in request)
- Response is encrypted before leaving gateway
- Only client with key can decrypt
- Gateway cannot read response content

---

## Closest Competitors

**Kong Konnect** is architecturally closest—they're adding MCP support and AI agent tooling. But they're enterprise infrastructure, not consumer/prosumer, and don't provide the security isolation model.

**mcp-agent's MCPAggregator** does tool aggregation but it's a library, not a hosted service.

**Nobody** is currently doing:

- Bioauth-gated OAuth aggregation
- Subdomain-isolated tool execution as a service
- Encrypted response channels for AI agents
- WebSpec-style discovery protocol (HEAD/OPTIONS)
- Provider/Service semantic distinction

---

## The Moat

<aside>
🪤

The moat is **security architecture + protocol design**.

Anyone can aggregate APIs. The defensible position is treating **the security of AI tool execution** as the primary feature, not an afterthought.

</aside>

This manifests as:

1. **Architectural decisions** that can't be retrofitted (subdomain isolation)
2. **Protocol extensions** to HTTP that feel native (HEAD/OPTIONS discovery)
3. **Trust model** that keeps secrets in user control (keychain + bioauth)
4. **Semantic layer** that enables natural language resolution (provider/service split)