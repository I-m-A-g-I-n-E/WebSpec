# Local Service Architecture (Client-Side)

<aside>
⚠️

**Implementation Note**: This section describes **client-side configuration** for local service isolation. Unlike the rest of WebSpec (which defines the protocol), this is a recommended architecture for implementing the local daemon on Unix-based systems. It is not required for WebSpec compliance, but provides security properties that mirror the web architecture.

</aside>

<aside>
🍎

**Platform Specificity**: This architecture is designed for **Unix-based systems** (Linux, macOS, BSD). macOS benefits most due to Apple's advanced security primitives (XPC, App Sandbox, entitlements). Windows implementations would require different primitives (named pipes, AppContainer) not covered here.

</aside>

## The Symmetry

The web's subdomain isolation model has a direct parallel in Unix:

| Web Primitive | Unix Primitive | Role |  |
| --- | --- | --- | --- |
| Subdomain | Socket path / XPC service | Isolation boundary |  |
| Same-origin policy | Filesystem permissions / Entitlements | Enforcement |  |
| Cookie scope | Socket ownership (uid/gid) | Auth boundary |  |
| `*.[gimme.tools](http://gimme.tools)` wildcard cert | Single router daemon | Trust anchor |  |
| Service tokens | Socket access + token validation | Capability proof |  |

The insight: **we can mesh with Unix just as we mesh with the web**.

---

## Architecture Overview

Instead of a monolithic [`localhost:7001`](http://localhost:7001) daemon, we create isolated services:

```
/var/run/gimme/
├── router.sock    ← Main entry point (like [api.gimme.tools](http://api.gimme.tools))
├── file.sock      ← File operations (sandboxed)
├── code.sock      ← Code execution (project-scoped)
├── shell.sock     ← Shell access (elevated confirmation)
└── app.sock       ← App control (accessibility permissions)
```

Each socket is a separate Unix domain socket, owned by a dedicated system user:

```bash
srwxr-x--- 1 _gimme_router _gimme    0 Dec 15 10:00 router.sock
srwxr-x--- 1 _gimme_file   _gimme    0 Dec 15 10:00 file.sock
srwxr-x--- 1 _gimme_code   _gimme    0 Dec 15 10:00 code.sock
srwxr-x--- 1 _gimme_shell  _gimme    0 Dec 15 10:00 shell.sock
srwxr-x--- 1 _gimme_app    _gimme    0 Dec 15 10:00 app.sock
```

---

## Token Routing

The `aud` claim determines which socket receives the request:

```json
{
  "aud": "[file.local.gimme.tools](http://file.local.gimme.tools)",
  "scope": ["GET:[file.local.gimme.tools/*](http://file.local.gimme.tools/*)"],
  "device_id": "device-abc123"
}
```

### Routing Table

| Token Audience | Routes To |
| --- | --- |
| [`local.gimme.tools`](http://local.gimme.tools) | `/var/run/gimme/router.sock` (legacy/fallback) |
| [`file.local.gimme.tools`](http://file.local.gimme.tools) | `/var/run/gimme/file.sock` |
| [`code.local.gimme.tools`](http://code.local.gimme.tools) | `/var/run/gimme/code.sock` |
| [`shell.local.gimme.tools`](http://shell.local.gimme.tools) | `/var/run/gimme/shell.sock` |
| [`app.local.gimme.tools`](http://app.local.gimme.tools) | `/var/run/gimme/app.sock` |

The cloud-side [`local.gimme.tools`](http://local.gimme.tools) maps the subdomain prefix to the appropriate local socket.

---

## Request Flow

```
Browser
   │
   ▼ HTTPS
[file.local.gimme.tools](http://file.local.gimme.tools) (cloud)
   │
   │ Validates: aud="[file.local.gimme.tools](http://file.local.gimme.tools)"
   │
   ▼ WebSocket tunnel
[localhost:7001](http://localhost:7001) (router daemon)
   │
   │ Routes based on aud claim
   │
   ▼ Unix socket (filesystem permissions)
/var/run/gimme/file.sock
   │
   ▼ Sandboxed process
gimme-file-service
   │
   └─▶ Can only access: ~/Documents, ~/Desktop
       Cannot: execute code, access shell, see other services
```

---

## Service Isolation Matrix

| Service | System User | Sandbox | Filesystem Access | Capabilities |
| --- | --- | --- | --- | --- |
| `gimme-file` | `_gimme_file` | App Sandbox | ~/Documents, ~/Desktop, ~/Downloads | Read/write files |
| `gimme-code` | `_gimme_code` | Project sandbox | Specified project directories only | Execute in sandbox |
| `gimme-shell` | user's own | None | Full home directory | Requires elevated confirmation |
| `gimme-app` | user's own | Accessibility | Application control | AppleScript, UI automation |

---

## macOS: XPC Services

On macOS, we can use XPC for even stronger isolation:

```
com.gimme.local.file    ← XPC service with file entitlements
com.gimme.local.code    ← XPC service with code entitlements
[com.gimme.local.shell](http://com.gimme.local.shell)   ← XPC service (requires TCC approval)
[com.gimme.local.app](http://com.gimme.local.app)     ← XPC service (requires Accessibility)
```

### Entitlements

```xml
<!-- com.gimme.local.file.entitlements -->
<plist version="1.0">
<dict>
    <key>[com.apple.security.app](http://com.apple.security.app)-sandbox</key>
    <true/>
    <key>[com.apple.security.files.user-selected.read](http://com.apple.security.files.user-selected.read)-write</key>
    <true/>
    <key>[com.apple.security.files.downloads.read](http://com.apple.security.files.downloads.read)-write</key>
    <true/>
</dict>
</plist>
```

```xml
<!-- com.gimme.local.code.entitlements -->
<plist version="1.0">
<dict>
    <key>[com.apple.security.app](http://com.apple.security.app)-sandbox</key>
    <true/>
    <key>[com.apple.security.temporary-exception.files.absolute-path.read](http://com.apple.security.temporary-exception.files.absolute-path.read)-write</key>
    <array>
        <string>/Users/*/Projects/</string>
    </array>
</dict>
</plist>
```

### XPC Connection Validation

```swift
// Router validates incoming XPC connections
func listener(_ listener: NSXPCListener, 
              shouldAcceptNewConnection connection: NSXPCConnection) -> Bool {
    // Verify the connecting process has the right entitlement
    let dominated = SecTaskCopyValueForEntitlement(
        SecTaskCreateFromSelf(nil)!,
        "com.gimme.capability.file" as CFString,
        nil
    )
    return dominated != nil
}
```

The OS itself enforces which processes can communicate with which services.

---

## Linux: Socket Permissions + Namespaces

On Linux, equivalent isolation uses:

### Socket Ownership

```bash
# Create dedicated users
sudo useradd -r -s /bin/false _gimme_file
sudo useradd -r -s /bin/false _gimme_code

# Socket permissions
chown _gimme_file:_gimme /var/run/gimme/file.sock
chmod 750 /var/run/gimme/file.sock
```

### Systemd Sandboxing

```
# /etc/systemd/system/gimme-file.service
[Service]
User=_gimme_file
Group=_gimme

# Filesystem isolation
ProtectHome=read-only
ReadWritePaths=/home/user/Documents /home/user/Downloads
ProtectSystem=strict

# Capability restrictions  
CapabilityBoundingSet=
NoNewPrivileges=true

# Namespace isolation
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
```

### Optional: User Namespaces

```bash
# Run service in isolated user namespace
unshare --user --map-root-user -- gimme-file-service
```

---

## Security Properties

### What This Achieves

| Attack Vector | Mitigation |
| --- | --- |
| Compromised file service tries to execute code | No execute permission, wrong socket |
| Compromised code service tries to read ~/Documents | Sandbox denies, wrong user |
| Token for file.local used against code.local | aud mismatch, rejected by router |
| Malicious website tries to connect | No tunnel access, no valid token |

### Inherited Security

Just as we inherit browser security on the web, we inherit:

| Platform | Inherited From |
| --- | --- |
| All Unix | 50+ years of filesystem permission hardening |
| Linux | Namespaces, cgroups, seccomp, systemd sandboxing |
| macOS | App Sandbox, XPC, Gatekeeper, TCC (Transparency, Consent, Control) |

---

## Comparison: Monolithic vs Isolated

### Monolithic (Simple)

```
[localhost:7001](http://localhost:7001)
    │
    ├── /file/*     ┐
    ├── /code/*     ├── All in one process
    ├── /shell/*    │   Same permissions
    └── /app/*      ┘
```

- ✅ Simple to implement
- ❌ Compromise of any capability = compromise of all
- ❌ Runs with user's full permissions

### Isolated (Recommended)

```
[localhost:7001](http://localhost:7001) (router)
    │
    ├── file.sock   ──▶ sandboxed, file-only
    ├── code.sock   ──▶ sandboxed, project-only  
    ├── shell.sock  ──▶ user perms, elevated confirm
    └── app.sock    ──▶ user perms, accessibility
```

- ✅ Mirrors web subdomain isolation exactly
- ✅ Compromise contained to single capability
- ✅ OS enforces boundaries
- ✅ Same token format, same mental model

---

## Configuration

Users can configure which local services are enabled:

```yaml
# ~/.config/gimme/local.yaml
services:
  file:
    enabled: true
    paths:
      - ~/Documents
      - ~/Downloads
      - ~/Desktop
    
  code:
    enabled: true
    project_roots:
      - ~/Projects
      - ~/Code
    
  shell:
    enabled: false  # Disabled by default - dangerous
    
  app:
    enabled: true
    allowed_apps:
      - "Visual Studio Code"
      - "Terminal"
      - "Finder"
```

---

## Implementation Status

`[TODO:IMPLEMENTATION]` This architecture is **recommended but not required** for WebSpec compliance. Implementers may choose:

1. **Full isolation** (recommended): Separate sockets, sandboxed services
2. **Partial isolation**: Single daemon with internal capability checks
3. **Monolithic**: Single daemon, relies on token scoping only

The protocol works identically in all cases—isolation is a defense-in-depth measure on the client side.