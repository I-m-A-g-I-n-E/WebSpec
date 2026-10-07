# WebSpec

**WebSpec turns every AI tool call into an ordinary HTTP request that says exactly what it
does, so the web, and a gateway in front of your MCP servers, can enforce it.**

REST APIs already work this way. GitHub's API deletes a repository with this request line:

```text
DELETE /repos/{owner}/{repo}
```

Browsers and proxies act on that line, and logs record it, without reading the body.

AI agents often reach their tools through MCP, the Model Context Protocol. Its
[Streamable HTTP transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
sends every call as a `POST` to one endpoint. Here is a delete, abridged:

```text
POST /mcp
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: delete_note

{"jsonrpc": "2.0", "id": 1,
 "method": "tools/call",
 "params": {
  "name": "delete_note",
  "arguments": {"id": "welcome"}}}
```

Since its 2026-07-28 revision, MCP copies the method and the tool's name into headers
(`Mcp-Method`, `Mcp-Name`), so a proxy can see which tool is called without reading the body.
But a name is only a name. A read, a send, and a delete are all a `POST` to the same endpoint,
so the rules that browsers and proxies apply by method, such as CORS preflights and method
allow-lists, treat them all alike.

A WebSpec gateway sits in front of unchanged MCP servers and gives each tool its own URL. Here
is the same delete, from a real exchange with WebSpec's reference gateway:

```text
DELETE /delete_note?id=welcome
Host: notes.localhost
X-Gimme-Definer: REMOVE
```

The gateway called `delete_note` on the demo MCP server it knows as `notes` (hence the host
`notes.localhost`) and answered `200 OK` ([full exchange](guide/walkthrough.md#level-0-local),
the last one under Level 0). That server has three tools: `read_note`, `send_note`, and
`delete_note`. Only the gateway speaks MCP. The agent's side is plain HTTP, so you can send the
same request with `curl` ([Quickstart](guide/index.md)).

The path is the tool's own name, so it repeats the verb. What WebSpec takes from REST is the
meaning of the method, not REST collection paths such as `/notes` and `/notes/welcome`, which
are only [proposed](#status). The `X-Gimme-Definer` header, the **definer**, names the act
again in one word, the way a check states its amount in both figures and words. A bank may
question or refuse a check whose two amounts disagree, and the gateway refuses a request whose
method and definer disagree. (Its `X-Gimme-` prefix, like the `X-WebSpec-` headers below, is
defined by this spec.)

## A tool call is a sentence

Each part of that request says one thing. The gateway learns what a tool does from the hints
its MCP server declares about it (annotations) or from the operator, never from the request.
`delete_note` declares itself destructive and idempotent, and a tool that declares nothing
counts as destructive, which is MCP's own default. The definer must come from the method's
family of verbs: `REMOVE` and `ARCHIVE` are `DELETE` verbs, and `SEND` and `CREATE` are `POST`
verbs. A `GET` carries none. Every status code below is a real answer from the reference
gateway.

| Part of the sentence | What the web already does, and what the gateway adds |
|---|---|
| **Who handles it** | The host, `notes.localhost`, names the **destination**: the MCP server `notes` behind the gateway. A browser treats each host as its own origin, so by default a script from another origin cannot read a destination's answers. It can still send a simple `GET`, which runs a read-only tool at level 0 without seeing the answer. Proxies route by host. A destination the gateway doesn't have, such as `mail.localhost`, gets `404 unknown_service`. |
| **What kind of act** | The method, `DELETE`. [HTTP requires](https://www.rfc-editor.org/rfc/rfc9110#section-9.2.1) a server to refuse a `GET` that would delete something, because crawlers and prefetchers `GET` every link. A browser sends a cross-origin `DELETE` only after a CORS preflight. The gateway sees the tool's contract, not its name: `delete_note` is destructive and idempotent, so the gateway admits `DELETE` and also `PUT`, HTTP's other idempotent write method. `GET /delete_note?id=welcome` gets HTTP's own refusal, `405 Method Not Allowed` with `Allow: HEAD, OPTIONS, PUT, DELETE`. `HEAD` and `OPTIONS` only describe a tool; they never run it. |
| **Which act exactly** | The path, `/delete_note`. Logs record it, and a reverse proxy can match it. The gateway calls the MCP tool of that name. |
| **The verb, said aloud** | The definer, `REMOVE`. A page from another origin can add a custom header only after a CORS preflight, so requiring one is a common defense against cross-site request forgery. The gateway refuses that preflight by default. A form posted to `/send_note` from another origin needs no preflight, but it arrives without a definer and gets `400 missing_definer`. `REMOVE` on `POST /send_note` gets `400 definer_family_mismatch`. The definer is checked against the method, not the tool: a `PUT` to `/delete_note` takes a `PUT` verb such as `SET`. |
| **With what** | The query, `id=welcome`, or a JSON body. Frameworks disagree about repeated query keys (HTTP parameter pollution). The gateway allows each query key once: `id=welcome&id=other` gets `400 duplicate_query_key`. Repeated keys inside a JSON body are not refused yet: the last one wins. |

**On whose authority.** The delete above went to a destination at level 0 of the
[security dial](#the-security-dial), which runs from 0 to 4. At level 0 nothing is signed, so
the gateway serves the destination only under loopback names such as `notes.localhost`. A
loopback name is only a `Host` header, though, and the gateway does not check where a request
comes from. Level 0 stays on this machine only while the gateway listens on `127.0.0.1`, its
default, and no proxy forwards those names ([known limits](spec/status.md#known-limits)).

A public domain requires level 1 or higher. From level 1, on any host, a shim in the agent's
harness (the program that runs the model and sends its calls) signs every request with a key it
shares with the gateway, much as GitHub and Stripe sign their webhooks with a shared secret. The
model never needs the key. The walkthrough runs `notes` at each level in turn. Here is a signed
read from its [level-1 run](guide/walkthrough.md#level-1-signed):

```text
GET /read_note?id=welcome
Host: notes.localhost
X-WebSpec-Nonce: 403c28aba42fa462a23469b97db9b204
X-WebSpec-Guard: ef56efa3
```

The gateway issued that nonce for `notes`, and it works once: the same bytes sent again get
`403 nonce_reused`. The guard is an HMAC over the method, host, path, query, body, definer, and
idempotency key, along with the nonce. Sign a `POST /send_note` whose body says
`"to": "ana@example.com"`, change the address to `eve@example.com`, and the gateway answers
`403 guard_invalid`. So does a request signed for one destination and sent to another. Tags
such as `ef56efa3` are 32 bits today, so rate-limit at the edge
([known limits](spec/status.md#known-limits)).

Browsers and proxies already enforce rules on the host and the method. The gateway checks every
part and makes sure the parts agree, so **a `GET` can reach only a tool whose contract says it
is read-only**, as the `405` above shows. The contract is what the server declared or the
operator set: a server that labels a tool read-only from its first listing is believed.

## WebSpec and MCP

WebSpec complements MCP; it does not replace it. MCP is how a client discovers and calls the
tools a server offers, and under WebSpec the gateway is that client. The agent speaks plain
HTTP to the gateway: `GET /` lists a destination's tools, `OPTIONS /{tool}` says what a call
needs, and each call is a request like the delete above. WebSpec is the HTTP face and the
firewall in front of the servers:

- Each MCP server becomes a destination (`{server}.{domain}`), each tool a path, and each call
  uses a method that fits what the tool does.
- A tool's MCP annotations (`readOnlyHint`, `destructiveHint`, …) become an **enforced
  contract** rather than a hint. MCP tells clients to treat annotations as untrusted unless the
  server is trusted. Under WebSpec the operator, whoever runs the gateway, makes that decision:
  adding a server trusts its annotations, and an override in the gateway's configuration
  corrects any tool the operator doesn't trust. Once the gateway has described or called a
  tool, it pins the strictest contract it has seen for it, so the server cannot re-list it as
  read-only to unlock `GET`. Pins live in memory, and a restart forgets them.
- The servers don't change. The reference gateway talks to them over MCP's own transports
  (stdio or HTTP).

## The security dial

Each destination runs at a **level** from 0 to 4. Raising the level only ever adds to what a
request must carry, and the model's part of the request stays the same at every level.

| Level | Adds | Holds against |
|---|---|---|
| **0** local | Method binding, definer verbs, and well-formed arguments | Confused or careless agents, and pages on other origins sending unsafe requests |
| **1** signed | The guard HMAC and a single-use nonce | Forged, altered, or replayed requests from anyone without the key |
| **2** bound | An idempotency key for tools that are not idempotent | Duplicate side effects from retries |
| **3** cleared | A single-use clearance for every call that changes something, and for risky reads | Calls the harness's policy did not vouch for, such as ones steered by prompt injection in data the agent read |
| **4** witnessed | A person's signature over the exact request, for the riskiest calls | A fully compromised agent harness, for the calls that need a person's signature |

Level 0's checks produced the refusals above. It checks the form of the arguments (each query
key once; a JSON body must be one object, with no `NaN`, `Infinity`, or numbers that overflow),
not the tool's schema. Level 0 is served only under loopback names. From level 1, a destination
may also be served on a public domain. Level 2's key travels in `Idempotency-Key`, the header
Stripe's API uses for safe retries. Level 2 also adds a bookend to the definer
(`SEND:8a9975c7`): a short HMAC that binds the method and verb to the first and last 16 bytes of
the body. Today it adds nothing, because the guard already signs the whole body and the definer.
The level-3 clearance is bound to the destination, method, tool, and arguments. It also covers
reads that their server or the operator marks `sensitive` or `dangerous`, WebSpec's tiers above
`open`. It is keyed with the guard's key, so it is a policy checkpoint, not a separate lock.
Level 4 covers destructive tools, tools marked `dangerous`, and open-world tools that change
something, such as sending a message.

The model writes only what it is good at: the method, host, path, arguments, and one definer
word. A harness shim adds the signatures, so the model never needs to see a key
([Security levels](spec/levels.md#the-harness-shim)).

## Status

What is built today:

| Area | State |
|---|---|
| Host routing (`{destination}.{domain}`, path = MCP tool name) | **Implemented** |
| Qualifier labels (`{qualifier}*.{destination}.{domain}`: optional labels before the destination, such as `eu` in `eu.mail.example.com`): parsing and allow-lists | **Implemented**, but every qualifier is refused today, even an allowed one, until qualifiers can route to their own backend |
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
- Transcripts from the reference gateway use loopback names such as `notes.localhost`. Other
  examples use `example.com`. The reference deployment serves `*.i-a-m.live` through a
  Cloudflare tunnel and Caddy, and `gimme.tools` is the planned product domain.
