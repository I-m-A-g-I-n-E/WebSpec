# A request at every level

This page shows what each security level adds, using a read, a send, and a delete. Each level
shows only the calls that illustrate its rules. The calls go to a demo MCP server with three
tools:

| Tool | Contract | Tier | Methods the gateway admits |
|---|---|---|---|
| `read_note` | read-only, closed world | `open` | `GET` |
| `send_note` | not read-only, not destructive, not idempotent, open world | `sensitive` | `POST`, `PATCH` |
| `delete_note` | destructive, idempotent, closed world | `sensitive` | `DELETE`, `PUT` |

The tier (`open` < `sensitive` < `dangerous`) is the tool's risk class
([TC-3](../spec/methods.md#tool-contracts)); each tool result reports it in `X-WebSpec-Tier`.

Every exchange below is real, but abridged: the transcripts show only the headers that matter
here, trim the `428` bodies to their main fields, and cut `X-WebSpec-Approval` values short. The
script `gateway/examples/walkthrough.py` runs the gateway in front of the demo server
(`gateway/examples/demo_server.py`) at each level, and signs the requests with the reference
shim (`gateway/examples/shim.py`). The shim is written from the
[spec](../spec/levels.md) rather than from the gateway's code, so each accepted request also
confirms that the two agree. To produce your own transcripts, run
`cd gateway && python examples/walkthrough.py`. The test suite replays the same scenarios on
every change and checks their status codes.

The model's side of every call is the same at every level: a method, a path, arguments, and
one [definer](how-it-works.md#the-verb-said-aloud-the-definer) verb. Everything else is added
by the shim, and each level adds to what the levels below it require.

## Level 0: local

At level 0 nothing is signed, and the destination is served only under loopback names. The
gateway still binds each method to the tool's contract, and marks what comes back:

- The read goes through `GET`.
- `GET` cannot reach the delete. The `405` answer lists the methods that can.
- The send needs a definer from `POST`'s verb family.
- The send reaches the open world, so its result comes back marked `X-UFO-Taint: open-world`,
  and the harness must treat it as untrusted data ([RS-2](../spec/methods.md#responses)).
- The answer echoes the verb it checked. `X-Gimme-Definer-Tier` (`definer_tier` in the body) is
  `1` when only the verb was checked, and `2` when a bookend was checked too, as it is from
  level 2. It is unrelated to the tool's tier in `X-WebSpec-Tier`.

**Read**

```text
GET /read_note?id=welcome
Host: notes.localhost

→ 200 OK
Cache-Control: no-store
X-WebSpec-Level: 0
X-WebSpec-Tier: open

{"result": "Hello from WebSpec."}
```

**A read can't reach a delete**

```text
GET /delete_note?id=welcome
Host: notes.localhost

→ 405 Method Not Allowed
Allow: HEAD, OPTIONS, PUT, DELETE

{"error": "method_not_allowed", "detail": "GET cannot invoke delete_note; its contract admits DELETE, PUT", "tool": "delete_note", "allowed": ["DELETE", "PUT"], "contract": {"read_only": false, "destructive": true, "idempotent": true, "open_world": false, "tier": "sensitive", "source": "annotations"}}
```

**Send**

```text
POST /send_note
Host: notes.localhost
Content-Type: application/json
X-Gimme-Definer: SEND

{"id":"welcome","to":"ana@example.com"}

→ 200 OK
Cache-Control: no-store
X-WebSpec-Level: 0
X-WebSpec-Tier: sensitive
X-UFO-Taint: open-world
X-Gimme-Definer-Canonical: SEND
X-Gimme-Definer-Tier: 1

{"result": {"queued": 1, "to": "ana@example.com"}, "definer_tier": 1, "canonical": "SEND"}
```

**Delete**

```text
DELETE /delete_note?id=welcome
Host: notes.localhost
X-Gimme-Definer: REMOVE

→ 200 OK
Cache-Control: no-store
X-WebSpec-Level: 0
X-WebSpec-Tier: sensitive
X-Gimme-Definer-Canonical: REMOVE
X-Gimme-Definer-Tier: 1

{"result": "deleted", "definer_tier": 1, "canonical": "REMOVE"}
```

## Level 1: signed

From level 1 on, every request carries a fresh nonce in `X-WebSpec-Nonce`, and in
`X-WebSpec-Guard` an HMAC over that nonce and the request's method, host, path, query, body,
definer, and idempotency key ([GD-2](../spec/levels.md#level-1-signed)). The shim fetches a new
nonce from `GET /__nonce` before every call, signing that fetch with an empty nonce. The first
transcript below shows one such fetch. The others leave the fetches out, so the nonce in the
signed read comes from a fetch that isn't shown. Sending exactly the same bytes a second time
fails, because the nonce has been spent.

**Nonce**

```text
GET /__nonce
Host: notes.localhost
X-WebSpec-Guard: ca89199e

→ 200 OK

{"nonce": "702ecdea0fcc48df6b340a21eb1c12a9", "audience": "notes", "expires_at": 1791244698.218, "ttl_seconds": 60}
```

**Read, signed**

```text
GET /read_note?id=welcome
Host: notes.localhost
X-WebSpec-Nonce: 403c28aba42fa462a23469b97db9b204
X-WebSpec-Guard: ef56efa3

→ 200 OK
Cache-Control: no-store
X-WebSpec-Level: 1
X-WebSpec-Tier: open

{"result": "Hello from WebSpec."}
```

**The same request again**

```text
GET /read_note?id=welcome
Host: notes.localhost
X-WebSpec-Nonce: 403c28aba42fa462a23469b97db9b204
X-WebSpec-Guard: ef56efa3

→ 403 Forbidden

{"error": "nonce_reused", "detail": "Nonce already used (single-use)"}
```

## Level 2: bound

At level 2 the definer of every unsafe method carries a bookend (`SEND:8a9975c7`), a short
HMAC over the method, the verb, and the ends of the body
([DF-2](../spec/methods.md#per-method-rules)), so the gateway now reports
`X-Gimme-Definer-Tier: 2`. The send, which is not idempotent, also carries an `Idempotency-Key`.
A retry with the same key replays the stored result, marked `Idempotent-Replayed: true`,
without running the tool again: the outbox still holds a single note. Reusing the key for a
different request is refused.

**Send**

```text
POST /send_note
Host: notes.localhost
Content-Type: application/json
X-Gimme-Definer: SEND:8a9975c7
Idempotency-Key: k-7f3a
X-WebSpec-Nonce: 720ecc008a06a0eef65c69b3ac184d0e
X-WebSpec-Guard: 27e12577

{"id":"welcome","to":"ana@example.com"}

→ 200 OK
Cache-Control: no-store
X-WebSpec-Level: 2
X-WebSpec-Tier: sensitive
X-UFO-Taint: open-world
X-Gimme-Definer-Canonical: SEND
X-Gimme-Definer-Tier: 2

{"result": {"queued": 1, "to": "ana@example.com"}, "definer_tier": 2, "canonical": "SEND"}
```

**Retry with the same key**

```text
POST /send_note
Host: notes.localhost
Content-Type: application/json
X-Gimme-Definer: SEND:8a9975c7
Idempotency-Key: k-7f3a
X-WebSpec-Nonce: 8bc2aa4c2859da898026a408c2aac0a1
X-WebSpec-Guard: 25da9432

{"id":"welcome","to":"ana@example.com"}

→ 200 OK
Cache-Control: no-store
Idempotent-Replayed: true
X-WebSpec-Level: 2
X-WebSpec-Tier: sensitive
X-UFO-Taint: open-world
X-Gimme-Definer-Canonical: SEND
X-Gimme-Definer-Tier: 2

{"result": {"queued": 1, "to": "ana@example.com"}, "definer_tier": 2, "canonical": "SEND"}
```

**Same key, different request**

```text
POST /send_note
Host: notes.localhost
Content-Type: application/json
X-Gimme-Definer: SEND:db370656
Idempotency-Key: k-7f3a
X-WebSpec-Nonce: f636af648bc7e20637b049ba30537014
X-WebSpec-Guard: e90ae9b8

{"id":"welcome","to":"bo@example.com"}

→ 422 Unprocessable Entity

{"error": "idempotency_key_reused", "detail": "This Idempotency-Key was already used for a different request."}
```

## Level 3: cleared

At level 3 the send needs a clearance: the harness's policy vouching for exactly this call
(destination, method, tool, and arguments), with a timestamp within 30 seconds of the
gateway's clock. When the policy declines, the shim sends the call without a clearance, and the
gateway refuses it. A policy might decline, for example, because the recipient's address came
from a web page the agent had just read. The reference shim vouches for every call unless it is
given a policy, so here the walkthrough makes it decline. The read needs no clearance, because
it is read-only and its tier is `open`.

**Send, not vouched for**

```text
POST /send_note
Host: notes.localhost
Content-Type: application/json
X-Gimme-Definer: SEND:8a9975c7
Idempotency-Key: c26b6bc0-df57-41fc-9496-f95b144662ac
X-WebSpec-Nonce: 785b1d88cc5ea5e3ba0fd6344691543f
X-WebSpec-Guard: 16e6b1bf

{"id":"welcome","to":"ana@example.com"}

→ 403 Forbidden

{"error": "clearance_missing", "detail": "Level 3 requires a valid, unspent X-UFO-Clearance token for POST send_note on notes"}
```

**Send, vouched for**

```text
POST /send_note
Host: notes.localhost
Content-Type: application/json
X-Gimme-Definer: SEND:8a9975c7
Idempotency-Key: 6764be5a-352f-4629-8162-f48dd4f3ccef
X-UFO-Clearance: 08db7b72:1791244639
X-WebSpec-Nonce: 6ebaa50f4841c06f55ad868cf8878d7a
X-WebSpec-Guard: df285a4b

{"id":"welcome","to":"ana@example.com"}

→ 200 OK
Cache-Control: no-store
X-WebSpec-Level: 3
X-WebSpec-Tier: sensitive
X-UFO-Taint: open-world
X-Gimme-Definer-Canonical: SEND
X-Gimme-Definer-Tier: 2

{"result": {"queued": 1, "to": "ana@example.com"}, "definer_tier": 2, "canonical": "SEND"}
```

**Read (tier open: no clearance needed)**

```text
GET /read_note?id=welcome
Host: notes.localhost
X-WebSpec-Nonce: cda32467f6b1b29a86748212afce3431
X-WebSpec-Guard: 252a527d

→ 200 OK
Cache-Control: no-store
X-WebSpec-Level: 3
X-WebSpec-Tier: open

{"result": "Hello from WebSpec."}
```

## Level 4: witnessed

At level 4 a person must witness both the send and the delete. The send reaches the open
world, and the delete destroys data. The first attempt at each gets a `428` with a summary of
exactly that request. A person signs the summary, and the shim retries the same request with
the approval attached (and, as always, a fresh nonce and guard). Here the signature comes from
`ssh-keygen -Y sign`, which is what `webspec-ctl approve` runs once the person has read the
summary and typed `approve` (see [Human approval](approval.md)). The read goes straight
through.

**Read (no approval needed)**

```text
GET /read_note?id=welcome
Host: notes.localhost
X-WebSpec-Nonce: c553d7bd908c983323b9020c1cfc2ba5
X-WebSpec-Guard: a737a4ee

→ 200 OK
Cache-Control: no-store
X-WebSpec-Level: 4
X-WebSpec-Tier: open

{"result": "Hello from WebSpec."}
```

**Send**

```text
POST /send_note
Host: notes.localhost
Content-Type: application/json
X-Gimme-Definer: SEND:8a9975c7
Idempotency-Key: k-91c2
X-UFO-Clearance: 08db7b72:1791244639
X-WebSpec-Nonce: f4f6fb3102e526d76c579449e8d62174
X-WebSpec-Guard: 78a34eca

{"id":"welcome","to":"ana@example.com"}

→ 428 Precondition Required

{
  "error": "approval_required",
  "challenge": "e6bcb018cdffb4cc91c2a460bab50b9e",
  "fingerprint": "4f935160084ad6289b3eb2b1216fd7079f09e6bddc8ea822d4507299599dbb84",
  "summary": {
    "v": 1,
    "method": "POST",
    "service": "notes",
    "host": "notes.localhost",
    "path": "/send_note",
    "tool": "send_note",
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
POST /send_note
Host: notes.localhost
Content-Type: application/json
X-Gimme-Definer: SEND:8a9975c7
Idempotency-Key: k-91c2
X-UFO-Clearance: 08db7b72:1791244639
X-WebSpec-Approval: e6bcb018cdffb4cc91c2a460…
X-WebSpec-Nonce: b712879c3c9d7b92a014bfc62aa34653
X-WebSpec-Guard: b85db549

{"id":"welcome","to":"ana@example.com"}

→ 200 OK
Cache-Control: no-store
X-WebSpec-Level: 4
X-WebSpec-Tier: sensitive
X-UFO-Taint: open-world
X-Gimme-Definer-Canonical: SEND
X-Gimme-Definer-Tier: 2

{"result": {"queued": 1, "to": "ana@example.com"}, "definer_tier": 2, "canonical": "SEND"}
```

**Delete**

```text
DELETE /delete_note?id=welcome
Host: notes.localhost
X-Gimme-Definer: REMOVE:f01b700a
X-UFO-Clearance: e899d774:1791244639
X-WebSpec-Nonce: 865692949b1057e4835adca62d4597ab
X-WebSpec-Guard: 02ccc86a

→ 428 Precondition Required

{
  "error": "approval_required",
  "challenge": "418f4e7144e73b825ee5c864553edb57",
  "fingerprint": "fd6d65a8ecab2be78260c5b9b721b3e5de2d27b8c899c945d22728f1b5462d39",
  "summary": {
    "v": 1,
    "method": "DELETE",
    "service": "notes",
    "host": "notes.localhost",
    "path": "/delete_note",
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
DELETE /delete_note?id=welcome
Host: notes.localhost
X-Gimme-Definer: REMOVE:f01b700a
X-UFO-Clearance: e899d774:1791244639
X-WebSpec-Approval: 418f4e7144e73b825ee5c864…
X-WebSpec-Nonce: da3dee7c219f2f8de87159866638408c
X-WebSpec-Guard: 6f828aa2

→ 200 OK
Cache-Control: no-store
X-WebSpec-Level: 4
X-WebSpec-Tier: sensitive
X-Gimme-Definer-Canonical: REMOVE
X-Gimme-Definer-Tier: 2

{"result": "deleted", "definer_tier": 2, "canonical": "REMOVE"}
```

## What the shim added

| Level | Added to the model's request |
|---|---|
| 0 | Nothing |
| 1 | `X-WebSpec-Nonce`, `X-WebSpec-Guard` |
| 2 | A bookend on the definer, and `Idempotency-Key` for non-idempotent tools |
| 3 | `X-UFO-Clearance`, when its policy vouches for the call |
| 4 | `X-WebSpec-Approval`, after a person signs |
