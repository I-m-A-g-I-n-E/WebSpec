# How it works

WebSpec makes each AI tool call an ordinary HTTP request whose parts say what the call does,
and puts a gateway in front of your MCP servers that checks those parts agree. This page
follows one request part by part, then shows where its authority comes from.

## One URL per tool

REST APIs already put the act on the request line. GitHub's API deletes a repository with:

```text
DELETE /repos/{owner}/{repo}
```

Browsers and proxies act on that line, and logs record it, without reading the body. MCP's
[Streamable HTTP transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
instead sends every call as a `POST` to one endpoint. Here is a delete, abridged:

```text
POST /mcp
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: delete_note

{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "delete_note", "arguments": {"id": "welcome"}}}
```

The `Mcp-Name` header lets a proxy see which tool is called. But a read, a send, and a delete
are all a `POST` to the same endpoint, so every rule the web applies by method, such as CORS
preflights and method allow-lists, treats them alike.

A WebSpec gateway sits in front of unchanged MCP servers and gives each tool its own URL. Here
is the same delete, from a real exchange with the reference gateway:

```text
DELETE /delete_note?id=welcome
Host: notes.localhost
X-Gimme-Definer: REMOVE
```

The gateway called `delete_note` on the demo MCP server it knows as `notes` and answered
`200 OK` ([full exchange](walkthrough.md#level-0-local)). That server has three tools:
`read_note`, `send_note`, and `delete_note`. Only the gateway speaks MCP. The agent's side is
plain HTTP, so `curl` works too ([Quickstart](index.md)).

What WebSpec takes from REST is the meaning of the method. The path is the tool's own name;
REST collection paths such as `/notes/welcome` are part of the long-range vision, not this
spec.

## A tool call is a sentence

The gateway learns what a tool does from the hints its MCP server declares (annotations) or
from the operator, never from the request. `delete_note` declares itself destructive and
idempotent. A tool that declares nothing counts as destructive, which is MCP's own default.
Every status code below is a real answer from the reference gateway.

| Part | What the web already does | What the gateway adds |
|---|---|---|
| **Who handles it**: the host, `notes.localhost` | A browser treats each host as its own origin, so a script from another origin cannot read a destination's answers. (It can still send a simple `GET`, which runs a read-only tool at level 0 without seeing the result.) Proxies route by host. | The host names the **destination**, the MCP server `notes`. A destination the gateway doesn't have, such as `mail.localhost`, gets `404 unknown_service`. |
| **What kind of act**: the method, `DELETE` | HTTP defines `GET` as [safe](https://www.rfc-editor.org/rfc/rfc9110#section-9.2.1), and crawlers and prefetchers `GET` every link. A browser sends a cross-origin `DELETE` only after a CORS preflight. | The tool's contract picks the methods. `delete_note` is destructive and idempotent, so it admits `DELETE` and `PUT`, HTTP's other idempotent write. `GET /delete_note?id=welcome` gets `405 Method Not Allowed` with `Allow: HEAD, OPTIONS, PUT, DELETE`. `HEAD` and `OPTIONS` describe a tool and never run it. |
| **Which act**: the path, `/delete_note` | Logs record it, and a reverse proxy can match it. | The gateway calls the MCP tool of that name. |
| **The verb, said aloud**: the definer, `REMOVE` | A page on another origin can add a custom header only after a CORS preflight, so requiring one is a common defense against cross-site request forgery. | The verb must come from the method's family: `REMOVE` and `ARCHIVE` are `DELETE` verbs; `SEND` and `CREATE` are `POST` verbs. The gateway refuses the cross-origin preflight by default. A form posted to `/send_note` from another origin needs no preflight, so it arrives without a definer and gets `400 missing_definer`. `REMOVE` on `POST /send_note` gets `400 definer_family_mismatch`. |
| **With what**: the query, `id=welcome`, or a JSON body | Frameworks disagree about repeated query keys (HTTP parameter pollution). | Each query key may appear once: `id=welcome&id=other` gets `400 duplicate_query_key`. Repeated keys inside a JSON body are not refused yet: the last one wins. |

The definer works like the amount on a check, written once in figures and again in words. A
bank may refuse a check whose two amounts disagree, and the gateway refuses a request whose
method and definer disagree. It checks the definer against the method, not the tool: a `PUT`
to `/delete_note` takes a `PUT` verb such as `SET`.

Browsers and proxies already enforce rules on the host and the method. The gateway checks every
part and makes sure the parts agree, so **a `GET` can reach only a tool whose contract says it
is read-only.**

## WebSpec and MCP

WebSpec complements MCP; it does not replace it. Under WebSpec the gateway is the MCP client,
and the agent speaks plain HTTP to the gateway: `GET /` lists a destination's tools,
`OPTIONS /{tool}` says what a call needs, and each call is a request like the delete above.

| MCP provides | WebSpec adds |
|---|---|
| A protocol for discovering and calling tools on a server | A URL and a method for each tool, so the web's own machinery (TLS, origins, CORS, proxies, logs) can see and police each call |
| Tool annotations, as hints | Annotations enforced as a contract, pinned so a server can't loosen it |
| Transports: stdio and Streamable HTTP | A gateway that speaks those transports to unchanged servers |
| Authorization for remote servers (OAuth) | Per-call authority that scales with risk: signatures, single-use clearances, and a person's approval |

MCP tells clients to treat annotations as untrusted unless the server is trusted. Under
WebSpec the operator, whoever runs the gateway, makes that decision. Adding a server trusts its
annotations, and an override in the gateway's configuration corrects any tool the operator
doesn't trust. A tool with no annotations gets MCP's strict defaults. Once the gateway has
described or called a tool, it pins the strictest contract it has seen for it, so the server
cannot re-list it as read-only to unlock `GET`. Pins live in memory, and a restart forgets
them. [Annotating MCP servers](annotating.md) shows how to annotate your own tools.

## On whose authority

At level 0 nothing is signed, so the gateway serves the destination only under loopback names
such as `notes.localhost`. A loopback name is only a `Host` header, though, and the gateway
does not check where a request comes from. Level 0 stays on this machine only while the gateway
listens on `127.0.0.1`, its default, and no proxy forwards those names
([known limits](../spec/status.md#known-limits)).

A public domain requires level 1 or higher. From level 1, on any host, a shim in the agent's
harness (the program that runs the model and sends its calls) signs every request with a key it
shares with the gateway, much as GitHub and Stripe sign their webhooks with a shared secret. The
model never needs the key. Here is a signed read from the walkthrough's
[level-1 run](walkthrough.md#level-1-signed):

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
such as `ef56efa3` are 32 bits today, so rate-limit at the edge.

## The security dial

Each destination runs at a level from 0 to 4. Raising the level only ever adds to what a
request must carry, and the model's part of the request stays the same at every level.

| Level | Adds | Holds against |
|---|---|---|
| **0** local | Method binding, definer verbs, and well-formed arguments | Confused or careless agents, and pages on other origins sending unsafe requests |
| **1** signed | The guard HMAC and a single-use nonce | Forged, altered, or replayed requests from anyone without the key |
| **2** bound | An `Idempotency-Key` for tools that are not idempotent, and a bookend on the definer | Duplicate side effects from retries |
| **3** cleared | A single-use clearance for every call that changes something, and for risky reads | Calls the harness's policy did not vouch for, such as ones steered by prompt injection in data the agent read |
| **4** witnessed | A person's signature over the exact request, for the riskiest calls | A fully compromised agent harness, for the calls that need a person's signature |

Two honest notes. The bookend, a short HMAC that binds the definer to the ends of the body,
adds nothing today, because the guard already signs the whole body and the definer. And the
level-3 clearance is keyed with the guard's key, so it is a policy checkpoint, not a separate
lock; independent signers per level are [proposed](../spec/status.md#roadmap).

## Next

- [A request at every level](walkthrough.md): the read, the send, and the delete at levels 0
  to 4, as real transcripts.
- [Security levels](../spec/levels.md): the rules each level adds.
- [Threat model](threat-model.md): what the rules hold against, and what they don't.
