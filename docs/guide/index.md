# Quickstart

In about ten minutes you'll run the reference gateway in front of a small MCP server, call its
tools with `curl`, watch it refuse a call, and then raise the security level and call it
through the reference shim. You need git and Python 3.11 or newer.

## 1. Install

```bash
git clone https://github.com/I-m-A-g-I-n-E/WebSpec && cd WebSpec
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

The gateway checks signed requests against a guard key. Nothing is signed until step 5, but the
gateway takes the key from its environment when it starts, so create one now:

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
annotations ([Annotating MCP servers](annotating.md)), and gives each tool a method and a path
from its name ([the route rule](how-it-works.md#the-route-rule)):

```console
$ curl -s http://notes.localhost:7001/ | python3 -m json.tool
{
    "service": "notes",
    "level": 0,
    "tools": [
        {
            "name": "read_note",
            "description": "Read a note.",
            "method": "GET",
            "path": "/note"
        },
        {
            "name": "send_email",
            "description": "Email a note to someone (simulated: queued in an in-memory outbox).",
            "method": "POST",
            "path": "/email",
            "definer": "SEND"
        },
        {
            "name": "delete_note",
            "description": "Delete a note.",
            "method": "DELETE",
            "path": "/note"
        }
    ]
}
```

Read a note with `GET`:

```console
$ curl -s "http://notes.localhost:7001/note?id=welcome"
{"result":"Hello from WebSpec."}
```

Now try to send an email with `GET`. Sending isn't read-only, so the gateway refuses, and
`allow` lists the methods `/email` takes:

```console
$ curl -si "http://notes.localhost:7001/email?id=welcome&to=eve@example.com"
HTTP/1.1 405 Method Not Allowed
date: Thu, 22 Oct 2026 21:25:06 GMT
server: uvicorn
allow: HEAD, OPTIONS, POST
content-length: 96
content-type: application/json
vary: Origin
cache-control: no-store

{"error":"method_not_allowed","detail":"/email does not map GET; it allows HEAD, OPTIONS, POST"}
```

Send it with `POST`. A `POST` names its action in the `WebSpec-Definer` header, one word from
`POST`'s verb family, here `SEND` ([the definer](how-it-works.md#which-action-on-post-the-definer)):

```console
$ curl -s -X POST http://notes.localhost:7001/email \
    -H 'WebSpec-Definer: SEND' -H 'Content-Type: application/json' \
    -d '{"id":"welcome","to":"ana@example.com"}'
{"result":{"queued":1,"to":"ana@example.com"},"canonical":"SEND"}
```

Leave the definer out and the gateway refuses:

```console
$ curl -s -X POST http://notes.localhost:7001/email \
    -H 'Content-Type: application/json' -d '{"id":"welcome","to":"ana@example.com"}'
{"error":"missing_definer","detail":"POST requires the WebSpec-Definer header"}
```

Ask what a call on a path needs before making it ([DS-1](../spec/methods.md#discovery)):

```console
$ curl -s -X OPTIONS http://notes.localhost:7001/email
{"service":"notes","level":0,"entries":[{"path":"/email","method":"POST","definer":"SEND","tool":"send_email", …,
 "contract":{"read_only":false,"destructive":false,"idempotent":false,"open_world":true,"tier":"sensitive","source":"annotations"},
 "requirements":{"guard":false,"definer":true,"idempotency_key":false,"empty_body":false,"clearance":false,"approval":false}}]}
```

## 5. Raise the security level

Add `"level": 2` to the `notes` entry in `webspec.json`. The gateway picks up the change
within 30 seconds. Unsigned requests are now refused:

```console
$ curl -s "http://notes.localhost:7001/note?id=welcome"
{"error":"guard_missing","detail":"WebSpec-Guard header required"}
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

notes.call("GET", "/note", {"id": "welcome"}).json()
# {'result': 'Hello from WebSpec.'}
notes.call("POST", "/email", {"id": "welcome", "to": "ana@example.com"}, "SEND").json()
# {'result': {'queued': 2, 'to': 'ana@example.com'}, 'canonical': 'SEND'}
notes.call("DELETE", "/note", {"id": "welcome"}).json()
# {'result': 'deleted'}
```

You gave the shim the method, the path, the arguments, and on `POST` the verb, and that is all
a model decides. The shim read the level and the routes from `OPTIONS /` and added
what level 2 requires: a single-use nonce and a guard (an HMAC over the request) on every call,
and an `Idempotency-Key` on the send, the one tool that isn't idempotent
([What the shim added](walkthrough.md#what-the-shim-added)).

Every tool call and refusal above is also in the audit log, `~/.webspec/gateway-audit.jsonl`,
each line chained to the one before it. Discovery that is answered (`GET /`, `OPTIONS`) and
nonce issuance are not logged ([AU-1](../spec/audit-deployment.md#audit)). Discovery the guard
refuses is logged, such as the shim's first, unsigned `OPTIONS /` at level 2.

## Next

- [How it works](how-it-works.md) explains what each part of a request says, and why.
- [A request at every level](walkthrough.md) shows what each level adds, with real transcripts
  of reads, sends, and deletes.
- [Annotating MCP servers](annotating.md) explains how your tools get the right contract.
- [Human approval](approval.md) sets up level 4.
- [Deploying](deploy.md) runs the gateway as its own user on Linux, macOS, or Docker, behind
  Caddy on Linux and in Docker.
- [Threat model and rationale](threat-model.md) explains why the rules are what they are.
