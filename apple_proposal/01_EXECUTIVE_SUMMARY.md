# WebSpec: A Web-Native Tool Invocation Protocol

**Proposal for Technical Review**  
*December 2024*

---

## The Core Thesis

Most protocols treat the web as a dumb pipe—serializing custom schemas over HTTP as mere transport. WebSpec takes the opposite approach: **the web IS the protocol**.

Every layer of WebSpec maps directly to an existing web primitive:

| Web Primitive | WebSpec Role |
|---------------|--------------|
| DNS | Service discovery |
| Subdomain | Isolation boundary |
| HTTP Method | Verb + permission boundary |
| Path | Object type system |
| Query params | Arguments |
| Same-origin policy | Capability boundary |
| Cookie scope | Auth boundary |
| TLS | Trust boundary |

This isn't architectural laziness—it's architectural humility. The browser's same-origin policy alone represents millions of engineering hours. We get it for the cost of *choosing subdomains correctly*.

---

## Why Inherited Security Beats Custom Security

Every feature inherited from web architecture is:

- **Code we don't write** → smaller attack surface
- **Bugs we don't create** → fewer vulnerabilities
- **CVEs we don't patch** → reduced maintenance burden
- **Edge cases already handled** → decades of battle-testing

Consider the alternative: MCP (Model Context Protocol) and similar approaches build custom isolation on top of HTTP. They must implement their own:

- Capability scoping (we inherit same-origin policy)
- Token validation (we inherit cookie security + standard JWT)
- Service isolation (we inherit subdomain boundaries)
- Permission patterns (we inherit HTTP method semantics)

Each custom implementation is a new attack surface. Each novel approach must be audited from scratch.

---

## The Protocol in 30 Seconds

A WebSpec request is just an HTTP request with semantic structure:

```
POST https://slack.gimme.tools/message?channel=general&body=Hello
 │          │                    │              │
 │          │                    │              └─ arguments (query params)
 │          │                    └─ object type (path)
 │          └─ service isolation (subdomain)
 └─ verb + permission boundary (HTTP method)
```

That's it. No custom envelope. No novel serialization. The URL *is* the invocation.

**Permission scoping becomes pattern matching:**

```
scopes: ["GET:slack.gimme.tools/message.*", "POST:slack.gimme.tools/message.*"]
```

**Gateway enforcement is six lines:**

```python
def authorize(token, request):
    pattern = f"{request.method}:{request.host}{request.path}"
    return any(fnmatch(pattern, scope) for scope in token['scope'])
```

---

## Local Execution: Meshing with Unix

The same philosophy extends to local tool execution. On macOS, we mesh with Apple's security architecture rather than fighting it:

| Web Primitive | macOS Primitive | Role |
|---------------|-----------------|------|
| Subdomain | XPC service identifier | Isolation boundary |
| Same-origin policy | Entitlements | Enforcement |
| Cookie scope | Socket ownership | Auth boundary |
| Service tokens | Entitlement + token validation | Capability proof |

**Proposed local architecture:**

```
com.gimme.local.file    ← App Sandbox, file entitlements only
com.gimme.local.code    ← Project sandbox, execute in isolation
com.gimme.local.shell   ← User permissions, elevated confirmation
com.gimme.local.app     ← Accessibility entitlement, UI automation
```

Each XPC service runs with minimum privileges. The OS enforces which processes can communicate with which services. A compromised file service cannot execute code—it doesn't have the entitlement.

**We're not asking Apple to build anything new.** We're demonstrating how to correctly use what already exists.

---

## What We're Asking

We are not seeking endorsement or adoption. We are seeking **technical review** from engineers who understand:

1. The security properties we're claiming to inherit
2. Where our assumptions about platform primitives might be wrong
3. What we're missing about TCC, code signing, or daemon lifecycle
4. Whether the XPC integration approach is sound

We have specific open questions (see attached) and welcome critical feedback.

---

## Specification Status

The full WebSpec specification is documented at:  
**https://www.notion.so/WebSpec-2c942c9038be80c2b26ee86a5ea677c5**

Current status:
- **Core protocol:** Complete (URL grammar, method semantics, type system)
- **Security architecture:** Complete (isolation model, token scoping, local bridge)
- **Authentication:** Complete (OAuth integration, keychain discovery)
- **Local service architecture:** Complete for Unix/macOS (XPC mapping, entitlements)
- **Reference materials:** In progress (formal grammar, examples)

---

## Contact

Preston Richey  
Orthonoetic Research  
[contact information]

---

*This document is part of a technical review packet. Full specification and threat model attached.*
