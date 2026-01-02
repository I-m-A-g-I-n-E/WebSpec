# The local.gimme.tools Bridge

The [`local.gimme.tools`](http://local.gimme.tools) subdomain bridges to [`localhost:7001`](http://localhost:7001) on the user's machine, enabling local tool execution with full WebSpec security.

<aside>
💡

**Why 7001?** Because it looks like "TOOL" in leet speak. Also, it's outside common port ranges, reducing conflicts.

</aside>

## The Problem

Some tools need local execution:

- File system access
- Running scripts
- Controlling applications
- Hardware interaction

Direct [`localhost`](http://localhost) access from browser has issues:

- No HTTPS (mixed content warnings)
- No authentication
- Firewall/NAT complications
- Different security model

## The Solution

[`local.gimme.tools`](http://local.gimme.tools) acts as a secure bridge:

```
Browser ───HTTPS───▶ [local.gimme.tools](http://local.gimme.tools) ───tunnel───▶ [localhost:7001](http://localhost:7001)
             │                              │
        Valid cert                     User's machine
        Same auth model                Local tool daemon
        Auditable                      Actual execution
```

The browser never talks to [localhost](http://localhost) directly.

---

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                         CLOUD                                │
│                                                               │
│   ┌──────────────────┐    ┌────────────────────┐            │
│   │  [auth.gimme.tools](http://auth.gimme.tools) │    │  [local.gimme.tools](http://local.gimme.tools)   │            │
│   │                  │    │                      │            │
│   │  Issues tokens   │    │  Validates token     │            │
│   │  with device-id  │    │  Routes to tunnel    │            │
│   └──────────────────┘    │  Returns stream URL  │            │
│                          └──────────┬───────────┘            │
│                                     │                         │
└─────────────────────────────────────┴─────────────────────────┘
                                       │
                              WebSocket tunnel
                          (outbound from user's machine)
                                       │
┌─────────────────────────────────────┬─────────────────────────┘
│                      USER'S MACHINE                           │
│                                                               │
│   ┌───────────────────────────────────────────────────────┐   │
│   │                     [localhost:7001](http://localhost:7001)                      │   │
│   │                                                         │   │
│   │   gimme-local daemon                                    │   │
│   │                                                         │   │
│   │   - Maintains outbound tunnel to [local.gimme.tools](http://local.gimme.tools)      │   │
│   │   - Validates incoming requests                         │   │
│   │   - Executes local tools                                │   │
│   │   - Streams results back                                │   │
│   │                                                         │   │
│   └───────────────────────────────────────────────────────┘   │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

---

## Connection Flow

### 1. Daemon Startup

```bash
# User starts the local daemon
$ gimme-local start

Listening on [localhost:7001](http://localhost:7001)
Connecting to [local.gimme.tools](http://local.gimme.tools)...
Tunnel established. Device ID: device-abc123
Ready for requests.
```

The daemon initiates an **outbound** WebSocket connection. No firewall configuration needed.

### 2. Device Registration

```
Daemon ──▶ [local.gimme.tools/register](http://local.gimme.tools/register)
         {
           "device_id": "device-abc123",
           "device_name": "Preston's MacBook",
           "capabilities": ["file", "code", "shell"]
         }

Server ◀── 
         {
           "tunnel_url": "wss://[local.gimme.tools/tunnel/device-abc123](http://local.gimme.tools/tunnel/device-abc123)",
           "session_key": "sk-xxx"
         }
```

### 3. Token with Device Binding

When user requests a local token:

```json
{
  "sub": "user-123",
  "aud": "[local.gimme.tools](http://local.gimme.tools)",
  "device_id": "device-abc123",
  "scope": [
    "GET:[local.gimme.tools/file.*](http://local.gimme.tools/file.*)",
    "POST:[local.gimme.tools/code.*](http://local.gimme.tools/code.*)"
  ],
  "exp": 1702603600
}
```

The `device_id` claim binds the token to a specific machine.

### 4. Request Flow

```
1. Client: POST [local.gimme.tools/code.python](http://local.gimme.tools/code.python)
   Authorization: Bearer {token with device_id}
   Body: {"script": "print('hello')"}

2. [local.gimme.tools](http://local.gimme.tools):
   - Validates token
   - Checks device_id matches active tunnel
   - Forwards request through WebSocket tunnel

3. [localhost:7001](http://localhost:7001):
   - Receives request
   - Validates session_key
   - Executes Python script
   - Streams output back

4. [local.gimme.tools](http://local.gimme.tools):
   - Relays stream to client
   - Returns streaming URL for long-running tasks

5. Client:
   - Receives immediate response or
   - Subscribes to stream URL for progress
```

---

## Security Model

### Why Outbound Tunnel?

| Approach | Firewall | Security | Complexity |
| --- | --- | --- | --- |
| Inbound port forward | Must configure | Exposes port | High |
| ngrok-style tunnel | No config | Encrypted, auditable | Low |

Outbound WebSocket means:

- User's firewall doesn't need modification
- All traffic is encrypted (WSS)
- [gimme.tools](http://gimme.tools) can audit/rate-limit
- Connection only exists when daemon is running

### Device Binding

Tokens are bound to specific devices:

```yaml
Token claims:
  device_id: "device-abc123"

Validation:
  - Token presented to [local.gimme.tools](http://local.gimme.tools)
  - Check: is device-abc123 currently connected?
  - Check: does user own device-abc123?
  - Only then forward to tunnel
```

Stolen token + wrong device = rejected.

### Local Confirmation

For sensitive operations, the daemon can require local confirmation:

```
┌──────────────────────────────────────────┐
│  gimme-local: Confirm action?         │
├──────────────────────────────────────────┤
│                                          │
│  DELETE /file/important-doc.pdf          │
│                                          │
│  Requested by: Claude (via [gimme.tools](http://gimme.tools))  │
│                                          │
│           [Allow]  [Deny]                 │
└──────────────────────────────────────────┘
```

---

## Available Local Tools

```yaml
objects:
  file:
    methods: [GET, POST, PUT, DELETE]
    description: Local filesystem access
    
  code:
    methods: [POST]
    types: [python, javascript, bash, ruby]
    description: Execute scripts locally
    
  shell:
    methods: [POST]
    description: Run shell commands
    
  app:
    methods: [POST]
    description: Control applications (open, close, interact)
    
  clipboard:
    methods: [GET, POST]
    description: Read/write clipboard
```

---

## Example Usage

```bash
# Read a local file
GET [https://local.gimme.tools/file/~/Documents/report.pdf](https://local.gimme.tools/file/~/Documents/report.pdf)

# Run a Python script
POST [https://local.gimme.tools/code.python](https://local.gimme.tools/code.python)
Body: {"script": "import pandas; print(pandas.__version__)"}

# Execute shell command
POST [https://local.gimme.tools/shell](https://local.gimme.tools/shell)
Body: {"command": "ls -la ~/Projects"}

# Open an application
POST [https://local.gimme.tools/app/open](https://local.gimme.tools/app/open)
Body: {"app": "Visual Studio Code", "file": "~/project/[main.py](http://main.py)"}
```

All with the same auth model, same permission scoping, same audit trail as cloud services.