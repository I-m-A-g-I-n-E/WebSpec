# WebSpec Threat Model

## Scope

This document describes the security properties WebSpec claims to provide, the threats it defends against, the threats it explicitly does NOT defend against, and the assumptions underlying our security model.

---

## Security Properties Claimed

### 1. Service Isolation

**Claim:** A compromised or malicious service cannot access data from other services.

**Mechanism:** Browser same-origin policy. Each service operates on a separate subdomain (`slack.gimme.tools`, `notion.gimme.tools`). The browser enforces isolation automatically.

**What this prevents:**
- Cross-service cookie theft
- Cross-service localStorage access
- Cross-service request interception
- Service impersonation

### 2. Token Audience Binding

**Claim:** A token issued for one service cannot be used at another service.

**Mechanism:** JWT `aud` claim bound to specific subdomain. Service validates `aud === self` before processing.

**What this prevents:**
- Token replay across services
- Privilege escalation via stolen token
- Confused deputy attacks

### 3. Blast Radius Containment

**Claim:** Compromise of one component limits damage to that component.

**Mechanism:** 
- Session cookies scoped to `auth.gimme.tools` only (HttpOnly, Secure, SameSite=Strict)
- Service tokens short-lived (1 hour typical)
- Local services isolated via Unix sockets / XPC with per-service entitlements

**Blast radius by compromise type:**

| Compromised Component | Blast Radius |
|-----------------------|--------------|
| Service token | One service, limited time |
| Service subdomain (XSS) | One service's scoped data |
| Session cookie | Cannot be stolen (HttpOnly + auth scope) |
| Local file service | File access only, no code execution |
| Local code service | Project directories only, no file service access |

### 4. Verifiable Permission Scope

**Claim:** Users can understand and verify what permissions they're granting.

**Mechanism:** Scope patterns use readable HTTP semantics:
- `GET:slack.gimme.tools/message.*` = "read Slack messages"
- `POST:slack.gimme.tools/message.*` = "send Slack messages"
- `DELETE:slack.gimme.tools/message.*` = "delete Slack messages"

**What this enables:**
- User-comprehensible permission prompts
- Auditable access logs
- Principle of least privilege enforcement

---

## Threats We Defend Against

### Network-Level
- **Eavesdropping:** TLS everywhere (HTTPS required)
- **Token interception:** Short-lived tokens, audience binding
- **Session hijacking:** HttpOnly cookies, auth subdomain isolation

### Application-Level
- **Cross-service attacks:** Same-origin policy isolation
- **Token misuse:** Audience validation, scope enforcement
- **Privilege escalation:** Per-service scoping, no ambient authority

### Local Execution (macOS)
- **Capability creep:** Separate XPC services with distinct entitlements
- **Sandbox escape:** Each service runs with minimum required permissions
- **Unauthorized access:** Token + entitlement double-check

---

## Threats We Do NOT Defend Against

### Explicitly Out of Scope

| Threat | Why Out of Scope |
|--------|------------------|
| **Compromised auth.gimme.tools** | Single point of trust; standard OAuth model |
| **Compromised user device** | Cannot protect against root/admin compromise |
| **Malicious service provider** | Service has legitimate access to its own data |
| **Social engineering** | User consents to permission prompt |
| **Supply chain attacks** | Requires trusted infrastructure |
| **Nation-state adversaries** | Not designed for this threat model |

### Platform Trust Assumptions

We assume the following are trustworthy:
- Browser same-origin policy implementation
- Operating system process isolation
- TLS certificate validation
- macOS XPC and entitlement enforcement
- DNS resolution (or use of certificate pinning)

If these assumptions are violated, WebSpec's security properties do not hold.

---

## Comparison: WebSpec vs. Custom Protocol

| Attack Vector | Custom Protocol | WebSpec |
|---------------|-----------------|---------|
| Cross-service data theft | Must implement isolation | Inherited from same-origin |
| Token replay | Must implement audience binding | Standard JWT + subdomain |
| Cookie theft | Must implement scoping | Browser-enforced |
| Permission creep | Must implement RBAC | HTTP method scoping |
| Local capability abuse | Must implement sandboxing | OS-enforced (XPC/entitlements) |

**Key insight:** Each "must implement" is an opportunity for implementation bugs. WebSpec minimizes novel code in security-critical paths.

---

## Assumptions

### Infrastructure Assumptions
1. `*.gimme.tools` TLS certificate is securely managed
2. `auth.gimme.tools` is operated with high security standards
3. DNS for `gimme.tools` is secured (DNSSEC or equivalent)
4. Service providers complete legitimate OAuth registration

### Platform Assumptions (macOS-specific)
1. XPC correctly enforces entitlement checks
2. App Sandbox provides effective isolation
3. Code signing prevents daemon tampering
4. TCC (Transparency, Consent, Control) prompts are trustworthy

### User Assumptions
1. Users read permission prompts before accepting
2. Users don't install malicious browser extensions
3. Users maintain device security (updates, malware protection)

---

## Known Limitations

### Current Specification
- No streaming/SSE support (planned for v1.1)
- No webhook callbacks for async operations (planned)
- Formal EBNF grammar in progress
- No Windows-specific local architecture yet

### Architectural
- Centralized trust in `auth.gimme.tools` (federation model under consideration)
- Requires user to trust gimme.tools as OAuth client
- Local execution requires daemon installation

---

## Open Questions for Apple Security Review

1. **TCC Integration:** What's the recommended flow for requesting Accessibility permissions for `gimme-app` service? Should this be a separate privileged helper?

2. **Daemon Lifecycle:** For the local bridge daemon, is launchd with `KeepAlive` appropriate, or should this be an App Extension?

3. **Code Signing:** What code signing requirements apply to XPC services that communicate with user-installed daemons?

4. **Entitlement Scope:** Are there existing entitlements we should use vs. custom private entitlements for inter-service capability scoping?

5. **Sandbox Profiles:** For `gimme-code` executing user scripts in project directories, is a custom sandbox profile the right approach, or should we use App Sandbox with temporary exceptions?

---

*This threat model accompanies the WebSpec Executive Summary and full specification.*
