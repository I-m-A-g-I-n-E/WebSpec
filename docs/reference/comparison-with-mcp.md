# Comparison with MCP

> **Summary**: MCP (Model Context Protocol) optimizes for **integration depth** within specific applications (like Claude Desktop or IDEs). WebSpec optimizes for **universal interoperability** across the entire web ecosystem. MCP builds a tunnel; WebSpec meshes with the fabric.

## The Architectural Divide

The fundamental difference is where the protocol lives in the stack.

### MCP: The "Tunnel" Approach

MCP treats the transport layer (stdio, SSE) as a pipe to send custom JSON-RPC messages through. It reinvents discovery, capability negotiation, and error handling within that pipe.

```json
// MCP: Custom protocol inside the pipe
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": { ... }
}
```

### WebSpec: The "Mesh" Approach

WebSpec treats the web itself as the protocol. It uses existing web primitives for discovery, negotiation, and errors.

```bash
# WebSpec: The pipe IS the protocol
POST /message.slack
```

---

## Feature Comparison

| Feature | MCP | WebSpec |
|---|---|---|
| **Transport** | JSON-RPC over Stdio / SSE | Standard HTTP / REST |
| **Verbs** | `tools/call`, `resources/read` | `GET`, `POST`, `DELETE`, `PATCH` |
| **Nouns** | Custom Resource URIs | Standard URLs |
| **Security Boundary** | Application Logic (Host enforcement) | Browser/OS Logic (Origin/User isolation) |
| **Discovery** | Manual config / Local registry | DNS / `OPTIONS` |
| **Client Support** | Requires MCP SDK | Universal (`curl`, Browser, Fetch) |

---

## Deep Dives

### 1. Security Models

**MCP** relies on the **Host Application** to be the security guardian. If you connect an MCP server to Claude Desktop, you are trusting Claude Desktop to manage permissions, sanitize inputs, and prevent the tool from doing harm. The security model is *software-defined*.

**WebSpec** relies on the **Platform** (Browser or OS).

- **Network:** Security is enforced by the Browser's Same-Origin Policy. `drive.gimme.tools` literally cannot read `slack.gimme.tools` cookies.
- **Local:** Security is enforced by Unix permissions. The file-system agent runs as a user that *cannot* read your SSH keys.

The security model is *infrastructure-defined*.

### 2. The "Client" Problem

**MCP** requires a "Client" (like Claude Desktop, Cursor, or an IDE) that implements the MCP spec. You cannot easily "curl" an MCP server or interact with it from a standard web app without an adapter.

**WebSpec** is client-agnostic.

- A shell script can use it (`curl -X POST ...`)
- A web app can use it (`fetch(...)`)
- An AI agent can use it (just standard tool calling)
- A human can use it (typing URL in browser)

### 3. Implementation Complexity

**MCP** is easier to implement for **single-purpose tools**. If you just want to expose a Python script to Claude, MCP's stdio transport is incredibly simple. No DNS, no SSL, no daemons.

**WebSpec** has higher **infrastructure overhead**. You need subdomains, certificates, and (for local tools) a daemon managing Unix sockets. It pays off at scale, but has a higher "Hello World" barrier.

---

## When to use which?

### Use MCP when:

- You are building a tool specifically for a desktop app (like an IDE plugin).
- You want zero infrastructure setup (just run a script).
- You don't care about interoperability with the broader web.
- You are working strictly over local stdio.

### Use WebSpec when:

- You want your tool to be usable by *any* agent, browser, or script.
- You need strong, platform-level security guarantees.
- You are building a service that lives on the open web.
- You believe the "browser is the operating system."
