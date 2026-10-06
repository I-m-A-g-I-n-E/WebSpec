# Quickstart

In about ten minutes you'll run the reference gateway in front of a small MCP server, call its
tools with `curl`, and then raise the security level and call it through the reference shim.
You need Python 3.11 or newer and a clone of the
[repository](https://github.com/I-m-A-g-I-n-E/WebSpec).

## 1. Install

```bash
cd WebSpec
python -m venv .venv && source .venv/bin/activate
pip install ./gateway
```

## 2. Point the gateway at an MCP server

The gateway reads MCP server definitions in the same format an MCP client uses. Save this as
`webspec.json`. It registers the guide's demo server, which has one read tool, one send tool,
and one delete tool:

```json
{
  "mcpServers": {
    "notes": {
      "command": "python",
      "args": ["gateway/examples/demo_server.py"]
    }
  }
}
```

With no `level` set, the destination runs at level 0, which is loopback only.

## 3. Start the gateway

```bash
openssl rand -hex 32 > guard.key                    # in real use, keep the key in your password manager
export WEBSPEC_GUARD_KEY=$(cat guard.key)
WEBSPEC_CONFIG=$PWD/webspec.json python -m webspec
```

The gateway listens on `127.0.0.1:7001`. Each MCP server becomes a destination under
`.localhost`. Here that is `notes.localhost:7001`.

## 4. Call it

In a second terminal, list the tools. Each tool comes with the methods its contract admits:

```console
$ curl -s http://notes.localhost:7001/
{"service":"notes","level":0,"tools":[
  {"name":"read_note","description":"Read a note.","methods":["GET"]},
  {"name":"send_note","description":"Send a note to someone (simulated: queued in an in-memory outbox).","methods":["PATCH","POST"]},
  {"name":"delete_note","description":"Delete a note.","methods":["DELETE","PUT"]}]}
```

Read a note with `GET`:

```console
$ curl -s "http://notes.localhost:7001/read_note?id=welcome"
{"result":"Hello from WebSpec."}
```

Try to reach the delete tool with `GET`. The gateway refuses and says which methods would work:

```console
$ curl -si "http://notes.localhost:7001/delete_note?id=welcome"
HTTP/1.1 405 Method Not Allowed
allow: HEAD, OPTIONS, PUT, DELETE
…
{"error":"method_not_allowed","detail":"GET cannot invoke delete_note; its contract admits DELETE, PUT", …}
```

Send, saying the verb aloud:

```console
$ curl -s -X POST http://notes.localhost:7001/send_note \
    -H 'X-Gimme-Definer: SEND' -H 'Content-Type: application/json' \
    -d '{"id":"welcome","to":"ana@example.com"}'
{"result":{"queued":1,"to":"ana@example.com"},"definer_tier":1,"canonical":"SEND"}
```

Leave the verb out and the gateway refuses with `missing_definer`. Ask what a tool needs before
calling it:

```console
$ curl -s -X OPTIONS http://notes.localhost:7001/send_note
```

The answer lists the contract, the methods, and the per-method requirements
([DS-1](../spec/methods.md#discovery)).

## 5. Turn the dial

Add `"level": 2` to the `notes` entry in `webspec.json`. The gateway picks up the change
within 30 seconds. Unsigned requests are now refused:

```console
$ curl -s "http://notes.localhost:7001/read_note?id=welcome"
{"error":"guard_missing","detail":"X-WebSpec-Guard header required"}
```

From here on, a harness shim signs the requests. The reference shim
(`gateway/examples/shim.py`) uses only the standard library. In the second terminal, from the
repository root with the virtual environment active, load the same key and start Python:

```bash
export WEBSPEC_GUARD_KEY=$(cat guard.key)
python
```

Then point the shim at the gateway:

```python
import os, sys, httpx
sys.path.insert(0, "gateway/examples")
from shim import Shim

notes = Shim(httpx.Client(base_url="http://127.0.0.1:7001"), host="notes.localhost:7001",
             destination="notes", key=bytes.fromhex(os.environ["WEBSPEC_GUARD_KEY"]))

notes.call("GET", "read_note", {"id": "welcome"}).json()
# {'result': 'Hello from WebSpec.'}
notes.call("POST", "send_note", {"id": "welcome", "to": "ana@example.com"}, "SEND").json()
# {'result': {'queued': 2, 'to': 'ana@example.com'}, 'definer_tier': 2, 'canonical': 'SEND'}
```

The calls are the same ones the model would write. The shim learned the level from
`OPTIONS`, and added the nonce, the guard, the bookend, and the `Idempotency-Key`.

Every call since step 4 is also in the audit log, `~/.webspec/gateway-audit.jsonl`, each line
chained to the one before it.

## Next

- [A request at every level](walkthrough.md) shows the same three calls at levels 0 to 4.
- [Annotating MCP servers](annotating.md) explains how your tools get the right contract.
- [Human approval](approval.md) sets up level 4.
- [Threat model and rationale](threat-model.md) explains why the rules are what they are.
