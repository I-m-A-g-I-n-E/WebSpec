# WebSpec

**WebSpec turns every AI tool call into an ordinary HTTP request that says exactly what it
does, so the web, and a gateway in front of your MCP servers, can enforce it.**

An agent sending an email through WebSpec doesn't make an opaque `tools/call`. It sends:

```http
POST /send_email HTTP/1.1
Host: mail.example.com
X-Gimme-Definer: SEND
Content-Type: application/json

{"to": "ops@example.com", "subject": "Deploy finished", "body": "All green."}
```

Who handles the call, what kind of act it is, which act exactly, and with what arguments each
have their own slot. Each slot is checked by something that already knows how to check it.

## A tool call is a sentence

| Part of the sentence | Slot | Example | Enforced by |
|---|---|---|---|
| Who handles it | Host (the destination label) | `mail.example.com` | DNS, TLS, origin and cookie scope, edge routing, and the gateway's audience binding |
| What kind of act | HTTP method | `POST` | HTTP semantics (safe, idempotent), CORS, edge policy, and the gateway's method binding |
| Which act exactly | Path (the tool) | `/send_email` | The gateway: the tool's contract |
| The verb, said aloud | `X-Gimme-Definer` | `SEND` | The gateway: the method's verb family |
| With what | Query (`GET`) or JSON body | `{"to": …}` | The gateway: strict JSON, unique keys |
| On whose authority | Signed headers | guard, clearance, approval | The gateway, at the destination's security level |

The higher a part sits in this table, the more of the existing web enforces it for free.
WebSpec puts each part where it can be enforced and makes sure the parts agree, so **a request
cannot describe itself as something weaker than it is.**

## WebSpec and MCP

WebSpec complements MCP; it does not replace it. MCP is how an agent discovers and calls the
tools a server offers. WebSpec is the HTTP face and the firewall in front of those servers:

- Each MCP server becomes a destination (`{server}.{domain}`), each tool a path, and each call
  uses a method that the tool's contract admits.
- A tool's MCP annotations (`readOnlyHint`, `destructiveHint`, …) become an **enforced
  contract** rather than a hint. MCP tells clients to treat annotations as untrusted unless the
  server is trusted. Under WebSpec the operator makes that decision, and the gateway remembers the
  strictest contract it has seen.
- The servers don't change. The reference gateway talks to them over MCP's own transports
  (stdio or HTTP).

WebSpec is not an MCP transport. MCP's Streamable HTTP sends JSON-RPC to a single endpoint.
WebSpec gives every tool its own URL and method, so proxies, logs, CORS, and people can see
what is being done.

## The security dial

Each destination runs at a **level** from 0 to 4. Raising the level only ever adds
requirements, and the model's part of the request stays the same at every level.

| Level | Name | Adds | Holds against |
|---|---|---|---|
| 0 | local | Method binding, definer verbs, strict arguments; served only under loopback names | Confused or careless agents reaching the wrong kind of act |
| 1 | signed | Guard HMAC over the method, host, path, query, body, definer, and idempotency key; single-use nonce bound to the destination | Forged, altered, or replayed requests from anyone without the key |
| 2 | bound | Payload bookend; `Idempotency-Key` for non-idempotent tools | Duplicate side effects from retries |
| 3 | cleared | A single-use clearance bound to destination, method, tool, and arguments | Sensitive calls that the harness's policy did not vouch for |
| 4 | witnessed | A person's signature over the exact request, for destructive, dangerous, and open-world mutating calls | A fully compromised agent harness, for the calls it witnesses |

The model writes only what it is good at: the method, host, path, arguments, and one definer
word. A harness shim adds the signatures, so no key ever enters the model's context
([Security levels](spec/levels.md#the-harness-shim)).

## Status

What is built today:

| Area | State |
|---|---|
| Host routing (`{destination}.{domain}`, path = MCP tool name) | **Implemented** |
| Qualifier labels (`{qualifier}*.{destination}.{domain}`): grammar and allow-lists | **Implemented** (allowed qualifiers fail closed until they route) |
| Tool contracts, contract pinning, and method binding | **Implemented** |
| Levels 0–4: guard, definers and bookend, `Idempotency-Key`, clearance, human approval | **Implemented** |
| Hash-chained audit log and process hardening | **Implemented** |
| Independent signers per level (RFC 9421 signatures), durable state, intent checks | **Proposed** |
| REST collection paths, format suffixes, hosted multi-tenant auth, discovery by meaning | **Proposed** (long-range vision, outside this spec) |

"Implemented" means built into the reference gateway (`gateway/` in the
[repository](https://github.com/I-m-A-g-I-n-E/WebSpec)) and covered by its tests. For the
details, see [Status and roadmap](spec/status.md).

## Reading this spec

- The **Spec** tab is short and normative. The **Guide** tab holds the walkthroughs,
  examples, and rationale.
- MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are used as in RFC 2119 and RFC 8174.
- Every requirement has a stable ID, such as [MB-1](spec/methods.md#method-binding). A gateway conforms at level *n* when it
  meets every requirement that applies at levels up to *n*
  ([conformance table](spec/status.md#conformance)).
- Examples use `example.com`. The reference deployment serves `*.i-a-m.live` through a
  Cloudflare tunnel and Caddy, and `gimme.tools` is the planned product domain.
