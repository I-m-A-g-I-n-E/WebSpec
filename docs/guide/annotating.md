# Annotating MCP servers

The gateway decides how each tool may be called from the tool's **contract**
([TC-1 to TC-6](../spec/methods.md#tool-contracts)). The contract comes from your server's MCP
annotations, unless an operator overrides it. Good annotations give agents the right methods
and keep sensitive tools behind the right gates. A tool with no annotations is treated as the
most dangerous kind of tool.

## What an unannotated tool costs

MCP's defaults are strict: a tool with no annotations counts as not read-only, destructive,
not idempotent, and open-world. The gateway uses those defaults as they are, so an
unannotated tool:

- can be called only with `DELETE` or `POST`, never with `GET`;
- needs an `Idempotency-Key` from level 2;
- needs a clearance at level 3;
- needs a person's signature at level 4.

That is the safe failure. For a tool that only reads, it is also an inconvenient one, so
annotate it.

## Choosing annotations

| Your tool… | Set | Methods it gets |
|---|---|---|
| only reads | `readOnlyHint: true` | `GET` |
| adds or changes something without destroying anything | `readOnlyHint: false`, `destructiveHint: false` | `POST`, `PATCH` |
| …and doing it twice has the same effect as doing it once | add `idempotentHint: true` | `POST`, `PATCH`, `PUT` |
| deletes or overwrites | `destructiveHint: true` (the default) | `DELETE`, `POST` |
| …and doing it twice has the same effect as doing it once | add `idempotentHint: true` | `DELETE`, `PUT` |

Set `openWorldHint: false` only when the tool touches nothing outside your own system. Sending
email, posting to chat, fetching a URL, and calling a third-party API are all open-world. An
open-world tool's output is marked `X-UFO-Taint: open-world`, and at level 4 every open-world
tool that is not read-only needs a person's signature. That is where a stolen secret would
leave.

With FastMCP:

```python
from fastmcp import FastMCP

mcp = FastMCP("notes")

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def read_note(id: str) -> str: ...

@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
def send_note(id: str, to: str) -> dict: ...

@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": True, "openWorldHint": False})
def delete_note(id: str) -> str: ...
```

Any MCP SDK works. What matters is what the server's `tools/list` returns:

```json
{"name": "send_note",
 "annotations": {"readOnlyHint": false, "destructiveHint": false, "openWorldHint": true}}
```

## Raising the tier

Tiers order tools by sensitivity: `open` < `sensitive` < `dangerous`. The gateway derives
`open` for read-only tools and `sensitive` for everything else. A server can raise a tool's
tier, but never lower it, with the `webspec/tier` key in the tool's `_meta`:

```python
@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False},
          meta={"webspec/tier": "sensitive"})
def read_secret(name: str) -> str: ...
```

A read-only tool raised to `sensitive` needs a clearance at level 3. A `dangerous` tool needs a
person's signature at level 4 whatever its other annotations say.

## Operator overrides

MCP says that annotations are hints, and that a client must not trust them from a server it
doesn't trust. The gateway trusts a server's annotations only because the operator chose to
configure that server. For a server you don't control, especially a remote one, state the
contract yourself in the gateway's configuration:

```json
{
  "mcpServers": {
    "tickets": {
      "type": "http",
      "url": "https://mcp.tickets.example/mcp",
      "level": 3,
      "tools": {
        "search_tickets": {"read_only": true, "open_world": false},
        "close_ticket": {"destructive": true, "idempotent": true, "tier": "sensitive"}
      }
    }
  }
}
```

An override is authoritative and may loosen what the server declared. Use that power
deliberately. The gateway guards against mistakes in two ways:

- **A typo fails closed.** An unknown key (`"readonly"`) or a value of the wrong type
  (`"read_only": "yes"`) turns the whole override into the strictest contract: destructive,
  non-idempotent, open-world, and `dangerous`.
- **Un-marking read-only starts from strict.** `"read_only": false` on a tool that the server
  marks read-only gives the strict defaults, not a read-only contract with one field flipped.

## When a server changes its annotations

Servers can re-list their tools at any time. The gateway remembers, for each tool, the
strictest contract it has seen ([TC-6](../spec/methods.md#tool-contracts)):

- A server that **tightens** a tool, for example by marking it destructive, takes effect
  immediately.
- A server that **loosens** a tool, for example by suddenly marking it read-only so that
  `GET` would reach it, is ignored. The gateway logs `Contract loosening blocked …`.
- Only an operator override loosens a contract. Removing a service from the configuration and
  adding it back does not reset what the gateway remembers. A restart does, because pins live in
  memory ([known limits](../spec/status.md#known-limits)).

## Check your work

`OPTIONS /{tool}` shows the contract that the gateway actually enforces, and where it came
from:

```console
$ curl -s -X OPTIONS http://notes.localhost:7001/send_note
{"name":"send_note", …, "methods":["PATCH","POST"],
 "contract":{"read_only":false,"destructive":false,"idempotent":false,
             "open_world":true,"tier":"sensitive","source":"annotations"}, …}
```

`source` is one of `annotations`, `default` (the server sent none), `override`, `pinned` (the
remembered contract merges listings from different sources), or `strictest` (an invalid
override).
