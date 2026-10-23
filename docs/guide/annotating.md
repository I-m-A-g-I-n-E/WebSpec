# Annotating MCP servers

The gateway decides which HTTP methods may call each tool, and what each call must carry, from
the tool's **contract**: whether the tool is read-only, destructive, idempotent, and open-world,
and its sensitivity tier ([TC-1 to TC-6](../spec/methods.md#tool-contracts)). The contract comes
from your server's MCP annotations, unless an operator overrides it.

## What an unannotated tool costs

MCP's defaults are strict: a tool with no annotations counts as not read-only, destructive,
not idempotent, and open-world. The gateway uses those defaults as they are. The
operator sets a [security level](how-it-works.md#the-security-dial) from 0 to 4 for each server,
and an unannotated tool:

- is reached only with `POST`, never with `GET`, whatever its name
  ([PA-4](../spec/addressing.md#the-route-rule));
- needs an `Idempotency-Key` from level 2;
- needs a single-use [clearance](../spec/levels.md#level-3-cleared) from level 3;
- needs a [person's signature](approval.md) at level 4.

That is the safe failure. For a tool that only reads, it is also an inconvenient one, so
annotate it.

## Choosing annotations

| Your tool… | Set | Methods its contract admits |
|---|---|---|
| only reads | `readOnlyHint: true` | `GET` |
| adds or changes something without destroying anything | `readOnlyHint: false`, `destructiveHint: false` | `POST`, `PATCH` |
| …and doing it twice has the same effect as doing it once | add `idempotentHint: true` | `POST`, `PATCH`, `PUT` |
| deletes or overwrites | `destructiveHint: true` (the default) | `DELETE`, `POST` |
| …and doing it twice has the same effect as doing it once | add `idempotentHint: true` | `DELETE`, `PUT` |

The route rule gives each tool one of those methods: the one the verb at the start of its name
asks for, if the contract admits it, or else a fallback, with the tool's full name as its path
([PA-4](../spec/addressing.md#the-route-rule)). A tool that isn't idempotent is never routed to
`PUT` or `DELETE`, so a `delete_` tool needs `idempotentHint: true` to be reached with
`DELETE`.

Set `openWorldHint: false` only when the tool touches nothing outside your own system. Sending
email, posting to chat, fetching a URL, and calling a third-party API are all open-world. For an
open-world tool:

- the response carries `WebSpec-Taint: open-world`, which tells the harness to treat the output as
  untrusted data ([RS-2](../spec/methods.md#responses));
- at level 4, every call needs a person's signature unless the tool is read-only
  ([RQ-1](../spec/methods.md#requirements)).

A read-only open-world tool, such as a fetcher or a search API, needs neither a clearance nor a
signature, yet it can carry data out in its arguments
([known limits](../spec/status.md#known-limits)). If it can reach arbitrary destinations,
[raise its tier](#raising-the-tier) to `dangerous`.

With FastMCP, here is how the demo server from the [Quickstart](index.md)
(`gateway/examples/demo_server.py`) annotates its tools:

```python
from fastmcp import FastMCP

mcp = FastMCP("notes")

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def read_note(id: str) -> str: ...

@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
def send_email(id: str, to: str) -> dict: ...

@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": True, "openWorldHint": False})
def delete_note(id: str) -> str: ...
```

Any MCP SDK works. What matters is what the server's `tools/list` returns:

```json
{"name": "send_email",
 "annotations": {"readOnlyHint": false, "destructiveHint": false, "openWorldHint": true}}
```

## Raising the tier

Tiers order tools by sensitivity: `open` < `sensitive` < `dangerous`. The gateway derives
`open` for read-only tools and `sensitive` for everything else. A server can raise a tool's
tier, but never lower it, with the `webspec/tier` key in the tool's `_meta` (in FastMCP, the
`meta=` argument):

```python
@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False},
          meta={"webspec/tier": "sensitive"})
def read_secret(name: str) -> str: ...
```

A tool whose tier is above `open` needs a clearance from level 3, even if it is read-only. A
`dangerous` tool also needs a person's signature at level 4, whatever its other annotations
say.

## Operator overrides

Adding a server to the gateway's configuration trusts its annotations
([WebSpec and MCP](how-it-works.md#webspec-and-mcp)). For a server you don't control, especially
a remote one, state the contract yourself in the
[configuration](../spec/audit-deployment.md#configuration):

```json
{
  "mcpServers": {
    "tickets": {
      "type": "http",
      "url": "https://mcp.tickets.example/mcp",
      "level": 3,
      "tools": {
        "search_tickets": {"read_only": true, "open_world": false, "tier": "open"},
        "close_ticket": {"read_only": false, "destructive": true, "idempotent": true,
                         "tier": "sensitive"}
      }
    }
  }
}
```

An override is authoritative and may loosen what the server declared. Three rules decide what
an override produces:

- **A typo fails closed.** An unknown key (`"readonly"`) or a value of the wrong type
  (`"read_only": "yes"`) turns the whole override into the strictest contract: destructive,
  non-idempotent, open-world, and `dangerous`.
- **Un-marking read-only starts from strict.** `"read_only": false` on a tool that the server
  marks read-only gives the strict defaults, not a read-only contract with one field flipped.
  An override without `read_only` keeps the server's value, so on a tool that the server marks
  read-only, the override's `destructive` and `idempotent` have no effect. For a server you
  don't trust, set `read_only` in every override.
- **The tier is not re-derived.** An override keeps the tier it starts from: an unannotated
  tool overridden to read-only stays `sensitive`. Set `tier` whenever you change `read_only`, as
  in the example above.

## When a server changes its annotations

Servers can re-list their tools at any time. Once the gateway has listed a tool,
it *pins* the tool's contract: it keeps the strictest contract it has seen for that tool
([TC-6](../spec/methods.md#tool-contracts)). For a pinned tool:

- A server that **tightens** a tool, for example by marking it destructive, takes effect the
  next time the gateway lists its tools: within 5 minutes, or on reconnect.
- A server that **loosens** a tool, for example by suddenly marking it read-only so that
  `GET` would reach it, is ignored. The gateway logs `Contract loosening blocked …`.
- Only an operator override loosens a pinned contract. Removing a service from the
  configuration and adding it back does not reset the pins. A restart does, because pins live
  in memory, and the gateway then trusts the first listing it sees. In the reference
  deployments a stdio server runs as the gateway's user, so it can force a restart. A tool
  re-listed under a new name is a new tool: it gets the contract its new listing claims, and no
  override for the old name applies ([known limits](../spec/status.md#known-limits)).

## Check your work

`OPTIONS /{noun}` shows, for each tool on that path, the contract that the gateway actually
enforces and where it came from. From level 1, discovery is signed like any other request, so
ask through the [Quickstart's shim](index.md#5-raise-the-security-level):
`notes.entry("POST", "/email", "SEND")["contract"]`. At level 0, an unsigned request will do:

```console
$ curl -s -X OPTIONS http://notes.localhost:7001/email
{"service":"notes","level":0,"entries":[{"path":"/email","method":"POST","definer":"SEND","tool":"send_email", …,
 "contract":{"read_only":false,"destructive":false,"idempotent":false,"open_world":true,"tier":"sensitive","source":"annotations"}, …}]}
```

If `source` is `default`, the gateway received no annotations for the tool: check what your
server's `tools/list` returns. [Discovery](../spec/methods.md#discovery) lists the other
values.
