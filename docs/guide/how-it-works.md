# How it works

WebSpec makes each AI tool call an ordinary HTTP request whose method says what kind of act it
is. A gateway in front of your MCP servers routes each request to one tool and refuses any
method that tool's contract doesn't admit. This page follows one request part by part, then
shows where its authority comes from.

## The act on the request line <a id="one-url-per-tool"></a>

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

A WebSpec gateway sits in front of unchanged MCP servers and gives each tool a method and a
path. Here is the same delete, from a real exchange with the reference gateway:

```text
DELETE /note?id=welcome
Host: notes.localhost
```

The gateway called `delete_note` on the demo MCP server it knows as `notes` and answered
`200 OK` ([full exchange](walkthrough.md#level-0-local)). That server has three tools:
`read_note`, `send_email`, and `delete_note`, and `curl` can call them all
([Quickstart](index.md)).

What WebSpec takes from REST is the meaning of the method.

## The route rule

The gateway gives each tool its method and path from the tool's name, and its contract decides
whether that method is allowed ([PA-4](../spec/addressing.md#the-route-rule)):

1. Split the name at its first `_`. `delete_note` gives `delete` and `note`.
2. If the name has an `_` and its first word is a verb of the rule, such as `get`, `read`,
   `send`, `create`, or `delete`, the verb picks the method, and the rest of the name is the
   path: `DELETE /note`. On `POST` the verb also picks the definer, so `send_email` is
   `POST /email` with `WebSpec-Definer: SEND`.
3. The method must be admissible: the tool's contract must admit it
   ([MB-1](../spec/methods.md#method-binding)), and a tool that isn't idempotent never gets
   `PUT` or `DELETE`. If the method isn't admissible, or step 2 doesn't apply, the tool keeps
   its full name as its path, with a method its contract admits. An unannotated
   `delete_draft`, which counts as not idempotent, is `POST /delete_draft`.

On the demo server the rule gives `/note` two tools and `/email` one, and the gateway answers
([full exchanges](walkthrough.md#level-0-local)):

| Request | Reaches | Answer |
|---|---|---|
| `GET /note?id=welcome` | `read_note` | `200 OK` |
| `DELETE /note?id=welcome` | `delete_note` | `200 OK` |
| `PUT /note` | no tool: `/note` maps only `GET` and `DELETE` | `405 Method Not Allowed` with `Allow: HEAD, OPTIONS, GET, DELETE` |
| `POST /email` with `WebSpec-Definer: SEND` | `send_email` | `200 OK` |

## A tool call is a sentence

The gateway learns what kind of act a tool performs, its **contract**, from the hints its MCP
server declares (annotations) or from the operator who runs the gateway, never from the
request. A tool that declares nothing counts as destructive, which is MCP's own default. The
gateway checks the method against the contract, and on `POST` the definer selects the action.
It checks arguments for form only. Every status code below is a real answer from the reference
gateway.

### Who handles it: the host

`notes.localhost` names the **destination**, the MCP server `notes`. A browser treats each host
as its own origin, so a script from another origin cannot read a destination's answers. (It can
still send a simple `GET`, which runs a read-only tool at [level 0](#the-security-dial) without
seeing the result.) Proxies route by host. A destination the gateway doesn't have, such as
`mail.localhost`, gets `404 unknown_service`.

### What kind of act: the method

The tool's contract, not the caller, picks the methods that may run it. HTTP defines `GET` as
[safe](https://www.rfc-editor.org/rfc/rfc9110#section-9.2.1), and crawlers and prefetchers
`GET` every link, so **the gateway lets a `GET` reach only a tool whose contract says it is
read-only.** `GET /email` gets `405 Method Not Allowed` with `Allow: HEAD, OPTIONS, POST`.

`delete_note` declares itself destructive and idempotent, so its contract admits `DELETE` and
`PUT`. The route rule gives each tool one of the methods its contract admits
([PA-5](../spec/addressing.md#the-route-rule)), here `DELETE`, which is why `PUT /note` is
refused above. A browser sends `PUT` or `DELETE` cross-origin only after a CORS preflight.
`HEAD` and `OPTIONS` describe a path and never run a tool.

### On what: the path <a id="which-act-the-path"></a>

`/note` names what the call acts on. Logs record the path, and a reverse proxy can match it.
The path is a single segment, so an id such as `welcome` is an argument, never part of the
path ([PA-1](../spec/addressing.md#path)). A path that maps no tool, such as `/note/welcome`,
gets `404 tool_not_found`.

### Which action, on POST: the definer <a id="the-verb-said-aloud-the-definer"></a>

`POST` covers many kinds of act, so a `POST` names its action in `WebSpec-Definer`, one verb
from `POST`'s family: `CREATE`, `SEND`, `INVOKE`, `TRIGGER`, or `UPLOAD`
([DF-1 to DF-3](../spec/methods.md#per-method-rules)). On `POST /email`, `SEND` selects
`send_email`. `CREATE` gets `400 unknown_action`, because `/email` maps no `CREATE`, and a
`DELETE` verb such as `REMOVE` gets `400 definer_family_mismatch`.

On `PUT`, `PATCH`, and `DELETE` the definer is optional and only confirms. A definer that is
sent must come from the method's family, and it is checked against the method, not the tool: a
`DELETE /note` with `ARCHIVE` still reaches `delete_note`, which deletes outright.

The header also matters across sites. A page on another origin can add a custom header only
after a CORS preflight, and the gateway refuses that preflight by default. A form posted to
`/email` from another origin needs no preflight, so it arrives without a definer and gets
`400 missing_definer`.

### With what: the arguments

`id=welcome` travels in the query; unsafe methods may also send a JSON body. Frameworks
disagree about repeated query keys (HTTP parameter pollution), so each key may appear once:
`id=welcome&id=other` gets `400 duplicate_query_key`.
Repeated keys inside a JSON body are not refused yet: the last one wins.

## WebSpec and MCP

WebSpec complements MCP; it does not replace it. Under WebSpec the gateway is the MCP client,
and the agent speaks plain HTTP to the gateway: `GET /` lists a destination's tools and their
routes, `OPTIONS /{noun}` says what a call on that path needs, and each call is a request like
the delete above.

| MCP provides | WebSpec adds |
|---|---|
| A protocol for discovering and calling tools on a server | A method and a path for each tool, so the web's own machinery (origins, CORS, proxies, logs) can see and police each call |
| Tool annotations, as hints | Annotations enforced as a contract, pinned so a server can't loosen it while the gateway runs |
| Transports: stdio and Streamable HTTP | A gateway that speaks those transports to unchanged servers |
| Authorization for remote servers (OAuth) | Per-call authority that scales with risk: signatures, single-use clearances, and a person's approval |

MCP tells clients to treat annotations as untrusted unless the server is trusted. Under
WebSpec the operator makes that decision. Adding a server trusts its annotations, and an
override in the gateway's configuration corrects any tool the operator doesn't trust. Once the
gateway has listed a tool, it pins the strictest contract it has seen for it, so the server
cannot re-list it as read-only to unlock `GET`. It also pins which tool each route reaches, so
a server cannot rename a tool onto another tool's route unnoticed
([PA-8](../spec/addressing.md#building-the-map)). A tool listed under a new name, on a route
no tool held, is a new tool, though, and gets the contract its listing claims. Pins live in
memory, so a restart forgets them. A stdio server runs as the gateway's user, so it can stop
the gateway, and the reference deployments then restart it with no pins
([known limits](../spec/status.md#known-limits)). [Annotating MCP servers](annotating.md) shows
how to annotate your own tools.

## On whose authority

At level 0 nothing is signed, so the gateway serves the destination only under loopback names
such as `notes.localhost`. A loopback name is only a `Host` header, though, and the gateway
does not check where a request comes from. Level 0 stays on this machine only while nothing
outside it can reach the gateway under a loopback name: the gateway listens on `127.0.0.1`
unless told otherwise, and a proxy in front of it must refuse loopback names that come from
outside ([DP-5, DP-8](../spec/audit-deployment.md#deployment)).

A public domain requires level 1 or higher. From level 1, on any host, a shim in the agent's
harness (the program that runs the model and sends its calls) signs every request with a key it
shares with the gateway, much as GitHub and Stripe sign their webhooks with a shared secret. The
model never needs the key. Here is a signed read from the walkthrough's
[level-1 run](walkthrough.md#level-1-signed):

```text
GET /note?id=welcome
Host: notes.localhost
WebSpec-Nonce: 4e77ef8d78b024923fb44820805d6547
WebSpec-Guard: 2702dbea
```

The gateway issued that nonce for `notes`, and it works once: the same bytes sent again get
`403 nonce_reused`. The guard is an HMAC over the method, host, path, query, body, definer, and
idempotency key, along with the nonce. Sign a `POST /email` whose body says
`"to": "ana@example.com"`, change the address to `eve@example.com`, and the gateway answers
`403 guard_invalid`. So does a request signed for one destination and sent to another. Tags
such as `2702dbea` are 32 bits today, so rate-limit at the edge.

## The security dial

Each destination runs at a level from 0 to 4. Raising the level only adds to what a request may
have to carry, and the model's part stays the same: level 1 adds the guard and a nonce, level 2
an `Idempotency-Key`, level 3 a clearance from the harness's policy, and level 4 a person's
signature. [Security levels](../spec/levels.md) says which calls need each, and
[Known limits](../spec/status.md#known-limits) says where each level falls short.

## Next

- [A request at every level](walkthrough.md): the read, the send, and the delete at levels 0
  to 4, as real transcripts.
- [Security levels](../spec/levels.md): the rules each level adds.
- [Threat model](threat-model.md): what the rules hold against, and what they don't.
