# Open Questions for Apple Security Engineering

These questions arise from our attempt to correctly integrate WebSpec's local execution model with macOS security primitives. We believe we're using the right building blocks but want to verify our approach.

---

## 1. TCC (Transparency, Consent, Control) Integration

**Context:** The `gimme-app` local service needs Accessibility permissions to automate applications via AppleScript/UI scripting.

**Our current understanding:**
- User grants Accessibility permission to the daemon/service
- This is a one-time system prompt
- Permission persists until revoked in System Preferences

**Questions:**
- Should Accessibility be granted to the main daemon or to the specific XPC service?
- Is there a preferred pattern for services that need TCC permissions but shouldn't run with full user privileges?
- Should we use a privileged helper tool (SMJobBless) for the Accessibility-requiring component?

---

## 2. Daemon Lifecycle Model

**Context:** The local bridge daemon (`gimme-local`) maintains a WebSocket tunnel to `local.gimme.tools` and routes requests to per-capability XPC services.

**Our current assumption:**
```xml
<!-- /Library/LaunchAgents/com.gimme.local.plist -->
<key>KeepAlive</key>
<dict>
    <key>NetworkState</key>
    <true/>
</dict>
<key>RunAtLoad</key>
<true/>
```

**Questions:**
- Is launchd the right model, or should this be an App Extension (e.g., Network Extension)?
- For a daemon that serves as an OAuth-authenticated tunnel, what's the recommended credential storage pattern?
- Should we consider a Login Item approach instead for better user visibility/control?

---

## 3. XPC Service Architecture

**Context:** We propose separate XPC services per capability:
```
com.gimme.local.file    ← File operations
com.gimme.local.code    ← Code execution  
com.gimme.local.shell   ← Shell access
com.gimme.local.app     ← App automation
```

**Our current understanding:**
- Each service has its own entitlements
- Services validate incoming connections via `shouldAcceptNewConnection`
- The router daemon dispatches based on token `aud` claim

**Questions:**
- Is XPC the right IPC mechanism for this, or should we consider Mach ports directly?
- For services that need different privilege levels (file vs. shell), should they be in the same bundle or separate?
- What's the recommended pattern for XPC services that are part of a non-App Store distributed tool?

---

## 4. Code Signing Requirements

**Context:** The daemon and XPC services will be distributed outside the App Store, likely via Homebrew or direct download.

**Our current understanding:**
- Developer ID signing required for Gatekeeper
- Notarization required for macOS 10.15+
- Hardened Runtime required for notarization

**Questions:**
- Are there additional signing requirements for XPC services specifically?
- For a daemon installed via `brew install`, what's the expected code signing flow?
- Should the XPC services be embedded in an app bundle or standalone?

---

## 5. Sandbox Profiles

**Context:** The `gimme-code` service needs to execute user scripts within project directories, but shouldn't have access to the full filesystem.

**Our proposed approach:**
```xml
<!-- com.gimme.local.code.entitlements -->
<key>com.apple.security.app-sandbox</key>
<true/>
<key>com.apple.security.temporary-exception.files.absolute-path.read-write</key>
<array>
    <string>/Users/*/Projects/</string>
</array>
```

**Questions:**
- Is a temporary exception the right approach, or should we use a custom sandbox profile?
- For executing arbitrary user code (Python, Node, etc.), are there additional hardened runtime considerations?
- Should we consider App Sandbox with user-selected directories (via powerbox) instead?

---

## 6. Entitlement Strategy

**Context:** We need to scope capabilities between services. A token for `file.local.gimme.tools` should only be usable by the file service.

**Our proposed approach:**
- Token `aud` claim specifies target service
- Router validates token, dispatches to correct XPC service
- XPC service validates it received from authorized router

**Questions:**
- Should we define custom private entitlements (e.g., `com.gimme.capability.file`) for inter-service authorization?
- Is there a pattern for XPC services to verify the identity of their client (the router)?
- How do we prevent a compromised service from impersonating the router?

---

## 7. Keychain Integration

**Context:** We use Keychain for OAuth token storage and propose using 1Password/system keychain for service discovery (not credential access).

**Our approach:**
- OAuth refresh tokens stored in Keychain (ACL: this app only)
- Keychain queried for domain presence (does user have credentials for slack.com?)
- Never read actual passwords—discovery only

**Questions:**
- Is querying Keychain for domain presence considered sensitive? Does it require special entitlements?
- For the 1Password integration (via 1Password Connect API), are there macOS-specific considerations?
- Should we use the Data Protection keychain accessibility classes?

---

## Summary

We're attempting to build a protocol that correctly inherits from macOS security primitives rather than reinventing them. We'd rather be told "you're using XPC wrong" than ship something that undermines the platform's security model.

We welcome any feedback, corrections, or suggestions for alternative approaches.

---

**Contact:**  
Preston Richey  
Orthonoetic Research  
[contact information]
