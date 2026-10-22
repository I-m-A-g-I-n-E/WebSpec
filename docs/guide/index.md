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
`webspec.json` in the repository root. It registers the guide's demo server, which has one
read tool, one send tool, and one delete tool:

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

## 3. Start the gateway

The gateway checks signed requests against a guard key. Create one now, even though nothing is
signed until step 5: without it, the send in step 4 fails
([Known limits](../spec/status.md#known-limits)).

```bash
# in real use, keep the key in your password manager
openssl rand -hex 32 > guard.key
export WEBSPEC_GUARD_KEY=$(cat guard.key)
WEBSPEC_CONFIG=$PWD/webspec.json python -m webspec
```

The gateway listens on `127.0.0.1:7001`. Each MCP server becomes a destination under
`.localhost`. Here that is `notes.localhost:7001`. Its entry sets no
[`level`](../spec/audit-deployment.md#configuration), so it runs at level 0, the lowest of five
security levels, where the gateway serves it only under loopback names like this one.

## 4. Call it

In a second terminal, list the tools. The gateway builds each tool's contract from its MCP
annotations ([Annotating MCP servers](annotating.md)), and each tool comes with the methods its
contract admits:

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

Send a note with `POST`. The `X-Gimme-Definer` header restates the method in one word from
`POST`'s verb family, here `SEND` ([the definer](how-it-works.md#the-verb-said-aloud-the-definer)):

```console
$ curl -s -X POST http://notes.localhost:7001/send_note \
    -H 'X-Gimme-Definer: SEND' -H 'Content-Type: application/json' \
    -d '{"id":"welcome","to":"ana@example.com"}'
{"result":{"queued":1,"to":"ana@example.com"},"definer_tier":1,"canonical":"SEND"}
```

Leave `X-Gimme-Definer` out and the gateway refuses with `400 missing_definer`.

Ask what a tool needs before calling it ([DS-1](../spec/methods.md#discovery)):

```console
$ curl -s -X OPTIONS http://notes.localhost:7001/send_note
{"name":"send_note", …, "methods":["PATCH","POST"],
 "contract":{"read_only":false,"destructive":false,"idempotent":false,"open_world":true, …},
 "level":0,"level_name":"local",
 "requirements":{"PATCH":{…},"POST":{"guard":false,"definer":true,"bookend":false,"idempotency_key":false, …}}, …}
```

## 5. Raise the security level

Add `"level": 2` to the `notes` entry in `webspec.json`. The gateway picks up the change
within 30 seconds. Unsigned requests are now refused:

```console
$ curl -s "http://notes.localhost:7001/read_note?id=welcome"
{"error":"guard_missing","detail":"X-WebSpec-Guard header required"}
```

From here on, the reference shim, `gateway/examples/shim.py`, signs the requests as an agent's
harness (the program that runs the model) would, so the model never needs the key. In the
second terminal, from the repository root, activate the virtual environment, load the same
key, and start Python:

```bash
source .venv/bin/activate
export WEBSPEC_GUARD_KEY=$(cat guard.key)
python
```

Then point the shim at the gateway. It connects to `127.0.0.1`, and `host` is the `Host`
header it signs and sends:

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

You gave the shim the same method, tool, arguments, and verb that you sent with `curl` at
level 0, and that is all a model decides. The shim read the level from `OPTIONS` and added
what level 2 requires: a single-use nonce and a guard (an HMAC over the request) on both calls,
plus a bookend (a short tag on the verb) and an `Idempotency-Key` on the send
([What the shim added](walkthrough.md#what-the-shim-added)).

Every tool call and refusal above is also in the audit log, `~/.webspec/gateway-audit.jsonl`,
each line chained to the one before it. Discovery (`GET /`, `OPTIONS`) and nonce issuance are
not logged ([AU-1](../spec/audit-deployment.md#audit)).

## Next

- [How it works](how-it-works.md) explains what each part of a request says, and why.
- [A request at every level](walkthrough.md) shows what each level adds, with real transcripts
  of reads, sends, and deletes.
- [Annotating MCP servers](annotating.md) explains how your tools get the right contract.
- [Human approval](approval.md) sets up level 4.
- [Deploying](deploy.md) runs the gateway as its own user on Linux, macOS, or Docker, behind
  Caddy on Linux and in Docker.
- [Threat model and rationale](threat-model.md) explains why the rules are what they are.
