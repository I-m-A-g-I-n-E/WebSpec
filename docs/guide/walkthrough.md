# A request at every level

This page shows what each security level adds, using a read, a send, and a delete. Each level
shows only the calls that illustrate its rules. The calls go to a demo MCP server with three
tools:

| Tool | Contract | Tier | Route |
|---|---|---|---|
| `read_note` | read-only, closed world | `open` | `GET /note` |
| `send_email` | not read-only, not destructive, not idempotent, open world | `sensitive` | `POST /email` with `SEND` |
| `delete_note` | destructive, idempotent, closed world | `sensitive` | `DELETE /note` |

The [route rule](how-it-works.md#the-route-rule) gives each tool its route from its name, and
the contract decides whether that method is allowed
([PA-4](../spec/addressing.md#the-route-rule)). The tier (`open` < `sensitive` < `dangerous`)
is the tool's risk class ([TC-3](../spec/methods.md#tool-contracts)); each tool result reports
it in `WebSpec-Tier`.

Every exchange below is real, but abridged: the transcripts show only the headers that matter
here, trim the `428` bodies to their main fields, and cut `WebSpec-Approval` values short. The
script `gateway/examples/walkthrough.py` runs the gateway in front of the demo server
(`gateway/examples/demo_server.py`) at each level, and signs the requests with the reference
shim (`gateway/examples/shim.py`). The shim is written from the
[spec](../spec/levels.md) rather than from the gateway's code, so each accepted request also
confirms that the two agree. To produce your own transcripts, run
`cd gateway && python examples/walkthrough.py`. The test suite replays the same scenarios on
every change and checks their status codes.

The model's side of every call is the same at every level: a method, a path, arguments, and
on `POST` one [definer](how-it-works.md#which-action-on-post-the-definer) verb. Everything else
is added by the shim, and each level adds to what the levels below it require.

## Level 0: local

At level 0 nothing is signed, and the destination is served only under loopback names. The
gateway still binds each method to the tool's contract, and marks what comes back:

- The read goes through `GET /note`.
- `/note` maps `GET` and `DELETE` only. A `PUT` gets `405`, and `Allow` lists the methods the
  path maps.
- The send is a `POST`, so it names its action with a definer from `POST`'s verb family:
  `SEND`. The answer echoes the verb it checked, as `canonical`.
- The send reaches the open world, so its result comes back marked `WebSpec-Taint: open-world`,
  and the harness must treat it as untrusted data ([RS-2](../spec/methods.md#responses)).
- The delete carries no definer. On `DELETE` it is optional.

**Read**

```text
GET /note?id=welcome
Host: notes.localhost

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 0
WebSpec-Tier: open

{"result": "Hello from WebSpec."}
```

**A method /note doesn't map**

```text
PUT /note
Host: notes.localhost
Content-Type: application/json

{"id":"welcome"}

→ 405 Method Not Allowed
Allow: HEAD, OPTIONS, GET, DELETE
Cache-Control: no-store

{"error": "method_not_allowed", "detail": "/note does not map PUT; it allows HEAD, OPTIONS, GET, DELETE"}
```

**Send**

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND

{"id":"welcome","to":"ana@example.com"}

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 0
WebSpec-Tier: sensitive
WebSpec-Taint: open-world

{"result": {"queued": 1, "to": "ana@example.com"}, "canonical": "SEND"}
```

**Delete**

```text
DELETE /note?id=welcome
Host: notes.localhost

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 0
WebSpec-Tier: sensitive

{"result": "deleted"}
```

## Level 1: signed

From level 1 on, every request carries a fresh nonce in `WebSpec-Nonce`, and in
`WebSpec-Guard` an HMAC over that nonce and the request's method, host, path, query, body,
definer, and idempotency key ([GD-2](../spec/levels.md#level-1-signed)). The shim fetches a new
nonce from `GET /__nonce` before every call, signing that fetch with an empty nonce. The first
transcript below shows one such fetch. The others leave the fetches out, so the nonce in the
signed read comes from a fetch that isn't shown. Sending exactly the same bytes a second time
fails, because the nonce has been spent.

**Nonce**

```text
GET /__nonce
Host: notes.localhost
WebSpec-Guard: ca89199e

→ 200 OK
Cache-Control: no-store

{"nonce": "fcf66f4ad6b315c105d47f369aa706cb", "audience": "notes", "expires_at": 1792699135.581, "ttl_seconds": 60}
```

**Read, signed**

```text
GET /note?id=welcome
Host: notes.localhost
WebSpec-Nonce: 4e77ef8d78b024923fb44820805d6547
WebSpec-Guard: 2702dbea

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 1
WebSpec-Tier: open

{"result": "Hello from WebSpec."}
```

**The same request again**

```text
GET /note?id=welcome
Host: notes.localhost
WebSpec-Nonce: 4e77ef8d78b024923fb44820805d6547
WebSpec-Guard: 2702dbea

→ 403 Forbidden
Cache-Control: no-store

{"error": "nonce_reused", "detail": "Nonce already used (single-use)"}
```

## Level 2: bound

At level 2 the send, which is not idempotent, carries an `Idempotency-Key`
([ID-1](../spec/levels.md#level-2-bound)). A retry with the same key replays the stored result,
marked `Idempotent-Replayed: true`,
without running the tool again: the outbox still holds a single note. Reusing the key for a
different request is refused.

**Send**

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND
Idempotency-Key: k-7f3a
WebSpec-Nonce: b4c0c2082b163858be6db723fe2903aa
WebSpec-Guard: e39897c9

{"id":"welcome","to":"ana@example.com"}

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 2
WebSpec-Tier: sensitive
WebSpec-Taint: open-world

{"result": {"queued": 1, "to": "ana@example.com"}, "canonical": "SEND"}
```

**Retry with the same key**

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND
Idempotency-Key: k-7f3a
WebSpec-Nonce: 22d4389f4c1e045da209ad47d8122bb9
WebSpec-Guard: ea2a288b

{"id":"welcome","to":"ana@example.com"}

→ 200 OK
Cache-Control: no-store
Idempotent-Replayed: true
WebSpec-Level: 2
WebSpec-Tier: sensitive
WebSpec-Taint: open-world

{"result": {"queued": 1, "to": "ana@example.com"}, "canonical": "SEND"}
```

**Same key, different request**

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND
Idempotency-Key: k-7f3a
WebSpec-Nonce: 249bb033da59352672d609ade9ad863d
WebSpec-Guard: b1579ea9

{"id":"welcome","to":"bo@example.com"}

→ 422 Unprocessable Entity
Cache-Control: no-store

{"error": "idempotency_key_reused", "detail": "This Idempotency-Key was already used for a different request."}
```

## Level 3: cleared

At level 3 the send needs a clearance: the harness's policy vouching for this call
(its destination, method, tool, and arguments), with a timestamp within 30 seconds of the
gateway's clock. When the policy declines, the shim sends the call without a clearance, and the
gateway refuses it. A policy might decline, for example, because the recipient's address came
from a web page the agent had just read. The reference shim vouches for every call unless it is
given a policy, so here the walkthrough makes it decline. The read needs no clearance, because
it is read-only and its tier is `open`.

**Send, not vouched for**

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND
Idempotency-Key: 59dac5c7-4879-4405-a370-f71615db7aad
WebSpec-Nonce: 0ed8d90b3c876aa8d8815e20847b8d66
WebSpec-Guard: 80709765

{"id":"welcome","to":"ana@example.com"}

→ 403 Forbidden
Cache-Control: no-store

{"error": "clearance_missing", "detail": "Level 3 requires a valid, unspent WebSpec-Clearance token for POST send_email on notes"}
```

**Send, vouched for**

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND
Idempotency-Key: cf9ca653-5096-45d1-928d-3a91268d4069
WebSpec-Clearance: d5727668:1792699077
WebSpec-Nonce: c1d077278ada3fbdf524f190c0ab7400
WebSpec-Guard: b426e6f9

{"id":"welcome","to":"ana@example.com"}

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 3
WebSpec-Tier: sensitive
WebSpec-Taint: open-world

{"result": {"queued": 1, "to": "ana@example.com"}, "canonical": "SEND"}
```

**Read (tier open: no clearance needed)**

```text
GET /note?id=welcome
Host: notes.localhost
WebSpec-Nonce: 38a24ad4007df7f51ce4610074909727
WebSpec-Guard: 692151a5

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 3
WebSpec-Tier: open

{"result": "Hello from WebSpec."}
```

## Level 4: witnessed

At level 4 a person must witness both the send and the delete. The send reaches the open
world, and the delete destroys data. The first attempt at each gets a `428` with a summary of
that request. A person signs the summary, and the shim retries the same request with
the approval attached (and, as always, a fresh nonce and guard). Here the signature comes from
`ssh-keygen -Y sign`, which is what `webspec-ctl approve` runs once the person has read the
summary and typed `approve` (see [Human approval](approval.md)). The read goes straight
through.

**Read (no approval needed)**

```text
GET /note?id=welcome
Host: notes.localhost
WebSpec-Nonce: e37db57741c460b3eb678c31841291ab
WebSpec-Guard: c964f568

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 4
WebSpec-Tier: open

{"result": "Hello from WebSpec."}
```

**Send**

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND
Idempotency-Key: k-91c2
WebSpec-Clearance: adcd9a53:1792699078
WebSpec-Nonce: 1761925b79ca4c74aa23ec7ce46d7c96
WebSpec-Guard: 95af1ea3

{"id":"welcome","to":"ana@example.com"}

→ 428 Precondition Required
Cache-Control: no-store

{
  "error": "approval_required",
  "challenge": "0f58b1b1e0d5c25623bc9c3501a4b94d",
  "fingerprint": "a7f83b4e4407c38945721d25b3491eed7feb56b614a14484ec94a18b888782a1",
  "summary": {
    "v": 1,
    "method": "POST",
    "service": "notes",
    "host": "notes.localhost",
    "path": "/email",
    "tool": "send_email",
    "args": {
      "id": "welcome",
      "to": "ana@example.com"
    },
    "body_sha256": "00f8f87d43343409d8bd484d75f05317569f4d37b7af69bfc41940b22aa12075"
  },
  "expires_in": 300
}
```

**Send again, with the approval**

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND
Idempotency-Key: k-91c2
WebSpec-Clearance: adcd9a53:1792699078
WebSpec-Approval: 0f58b1b1e0d5c25623bc9c35…
WebSpec-Nonce: 6c791dc64a0d4528b1a925cba9e999f5
WebSpec-Guard: 6134de74

{"id":"welcome","to":"ana@example.com"}

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 4
WebSpec-Tier: sensitive
WebSpec-Taint: open-world

{"result": {"queued": 1, "to": "ana@example.com"}, "canonical": "SEND"}
```

**Delete**

```text
DELETE /note?id=welcome
Host: notes.localhost
WebSpec-Clearance: 003927b8:1792699078
WebSpec-Nonce: 7b4e493a6243f06a57dcc4396705c6a6
WebSpec-Guard: 374f8442

→ 428 Precondition Required
Cache-Control: no-store

{
  "error": "approval_required",
  "challenge": "ec0645df42623ebe2b049b3364c05d02",
  "fingerprint": "1fcc3777da37a569522691778b6218792e6a002321ce7646cb6c8ed163771171",
  "summary": {
    "v": 1,
    "method": "DELETE",
    "service": "notes",
    "host": "notes.localhost",
    "path": "/note",
    "tool": "delete_note",
    "args": {
      "id": "welcome"
    },
    "body_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  },
  "expires_in": 300
}
```

**Delete again, with the approval**

```text
DELETE /note?id=welcome
Host: notes.localhost
WebSpec-Clearance: 003927b8:1792699078
WebSpec-Approval: ec0645df42623ebe2b049b33…
WebSpec-Nonce: ea3b2e1e241cb5e3bceec737de0194a4
WebSpec-Guard: 1a568c6e

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 4
WebSpec-Tier: sensitive

{"result": "deleted"}
```

## What the shim added

| Level | Added to the model's request |
|---|---|
| 0 | Nothing |
| 1 | `WebSpec-Nonce`, `WebSpec-Guard` |
| 2 | `Idempotency-Key` for non-idempotent tools |
| 3 | `WebSpec-Clearance`, when its policy vouches for the call |
| 4 | `WebSpec-Approval`, after a person signs |
