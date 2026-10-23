---
name: Authentication & Authorization
description: This skill should be used when checking WebSpec security levels 0 to 4 and the code that meets them - the guard (WebSpec-Nonce, WebSpec-Guard HMAC), Idempotency-Key and replays, the level-3 clearance (WebSpec-Clearance), level-4 human approval (428 challenge, WebSpec-Approval, webspec-ctl approve), commit order, guard-key custody, and the harness shim. Trigger phrases include "security level", "guard", "nonce", "HMAC", "Idempotency-Key", "clearance", "approval", "428", "harness shim", "guard key".
version: 2.0.0
---

# WebSpec security levels

This skill follows the normative spec page
[Security levels](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/levels/) (rules `LV`, `PO`,
`GD`, `ID`, `CL`, `AP`, `CO`, `SH`) and the
[known limits](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/status/#known-limits). Cite a rule
by its ID in every finding. Where this summary and the spec differ, the spec wins.

| Level | Name | Adds |
|---|---|---|
| 0 | local | Method binding, definers, strict arguments. Loopback names only (HG-7) |
| 1 | signed | Guard HMAC over the method, host, path, query, body, definer, and idempotency key, with a single-use nonce bound to the destination |
| 2 | bound | `Idempotency-Key` for every non-idempotent tool |
| 3 | cleared | Single-use clearance for every tool that is not read-only and every tool above tier `open` |
| 4 | witnessed | A person's signature over the request's method, destination, path, arguments, and body, for destructive tools, `dangerous` tools, and open-world mutations |

- **LV-1** The level comes from the destination's config key `level`. Without it, `guard:
  true` means level 1, else level 0. An invalid value fails closed to level 4.
- **RQ-2** Each level only adds requirements.

## Level 1: the guard (GD-1 to GD-5)

- **GD-1** Every request carries `WebSpec-Nonce` (a nonce issued for that destination, unused,
  under 60 s old) and `WebSpec-Guard` (the HMAC tag). Errors: `401 guard_missing`,
  `401 nonce_missing`; `403 guard_invalid`, `unknown_nonce`, `nonce_reused`, `nonce_expired`,
  `nonce_audience_mismatch`.
- **GD-2** The tag is the hex of the first 4 bytes of `HMAC-SHA256(guard key, message)`:

    ```text
    METHOD ":" host ":" path ":" nonce ":" hex(sha256(body))
      [ ":?" canonical-query ]   ; only if the request has a query
      [ ":!" definer ]           ; only if the definer is not empty
      [ ":#" idempotency-key ]   ; only if Idempotency-Key is present
    ```

    `host` is the `Host` header as received, port included. `path` is the decoded path with
    its surrounding slashes trimmed, prefixed by one `/`. `definer` is the raw
    `WebSpec-Definer` value. No header name enters the message.
- **GD-3** Canonical query: decode as a form would, sort by key then value, re-encode every
  character except `A–Z a–z 0–9 - . _ ~` (a space becomes `%20`), join with `&`.
- **GD-4** `GET /__nonce`, signed with an empty nonce, returns `{"nonce", "audience",
  "expires_at", "ttl_seconds"}` with `Cache-Control: no-store`. A nonce is bound to its
  destination, valid for 60 s, and usable once.
- **GD-5** The guard key comes from outside the gateway's code and config:
  `WEBSPEC_GUARD_KEY` (64 hex digits or a passphrase) or the file `WEBSPEC_GUARD_KEY_FILE`
  names. Without a usable key, requests that need it get a plain-text `500`.

## Level 2: Idempotency-Key (ID-1 to ID-5)

- **ID-1** Every unsafe request to a non-idempotent tool carries `Idempotency-Key`
  (`400 idempotency_key_required`), 1 to 255 visible ASCII characters
  (`400 idempotency_key_invalid`). The route rule never sends such a tool to `PUT` or `DELETE`,
  so in practice this is `POST` and `PATCH`.
- **ID-2** Keys are per destination. The gateway keeps each key with its request fingerprint
  and the tool the request resolved to. The same key with another tool (such as a `POST` with
  another definer) counts as a different request.
- **ID-3** A key seen before: same request and completed → the stored response with
  `Idempotent-Replayed: true`, tool not called; different request → `422
  idempotency_key_reused`; still running → `409 idempotency_key_in_flight`; outcome unknown →
  `409 idempotency_outcome_unknown` (check the outcome, then use a new key); result too large
  → `409 idempotency_result_not_replayable`.
- **ID-4** A replay is answered before the clearance and the approval are checked.
- **ID-5** In-flight and outcome-unknown records are never evicted; a full store is
  `503 idempotency_store_full`.

## Level 3: the clearance (CL-1 to CL-3)

- **CL-1** `WebSpec-Clearance: tag:ts`, where `tag` is the first 4 bytes, in hex, of

    ```text
    HMAC-SHA256(key, "ufo2:" destination ":" METHOD ":" tool ":" canonical-args ":" ts)
    ```

    `tool` is the tool the request resolves to (from the route map), not the path.
    `canonical-args` is the arguments object as canonical JSON (sorted keys, `,` and `:`
    separators, non-ASCII escaped as `\uXXXX`). The `ufo2:` prefix is kept for byte stability.
- **CL-2** Valid for 30 s either side of `ts`, spent once. Errors: `403 clearance_missing`,
  `clearance_malformed`, `clearance_expired`, `clearance_invalid`, `clearance_reused`.
- **CL-3** Checked early, spent only at commit.

The reference gateway keys the clearance with the guard key, so it is a policy checkpoint,
not a separate lock.

## Level 4: human approval (AP-1 to AP-7)

- **AP-1** A request that needs approval and has no `WebSpec-Approval` gets
  `428 approval_required` with a `challenge`, a `summary` (method, destination, host, path,
  tool, arguments, body hash), its `fingerprint`, and the `sign_message`. The fingerprint does
  not cover the definer or the idempotency key.
- **AP-2** An approval tool re-derives the fingerprint from the summary it shows, escapes
  control and non-ASCII characters, reads the confirmation from the terminal (never stdin),
  and signs in the SSH namespace `webspec-approval`. The reference tool is
  `webspec-ctl approve`.
- **AP-3** The client retries the same method, path, arguments, and body with a fresh nonce
  and guard, a clearance minted within 30 s, and
  `WebSpec-Approval: <challenge>:<base64 SSH signature>`. The gateway verifies it with
  `ssh-keygen -Y verify` against `WEBSPEC_APPROVERS_FILE`.
- **AP-4 to AP-7** A challenge lasts 300 s and is single-use; pending challenges are never
  evicted (`429 approval_queue_full`); no approvers configured is
  `503 approval_unavailable`.

## Commit and the shim

- **CO-1** The clearance and the approval are spent, and the idempotency key claimed,
  immediately before the call, after every check has passed. A nonce is spent as soon as its
  tag verifies (PO-2), so every attempt needs a fresh one.
- **SH-1** The model writes only the method, host, path, arguments, and definer verb. A shim
  in the harness adds nonces, guard tags, idempotency keys, and clearances, keeps its keys
  where the model cannot read them, and takes no instructions from text.
- **SH-2** The shim learns the route map, and what each entry requires, from `OPTIONS /`.
  The reference shim is `gateway/examples/shim.py`.

## Real exchanges

These are real answers of the reference gateway (`gateway/`) in front of the demo server
(`gateway/examples/demo_server.py`), abridged to the headers that matter. The signed requests
come from the reference shim (`gateway/examples/shim.py`). [A request at every
level](https://i-m-a-g-i-n-e.github.io/WebSpec/guide/walkthrough/) shows every level in full.

At level 1, a request without the guard:

```text
GET /note?id=welcome
Host: notes.localhost

→ 401 Unauthorized
Cache-Control: no-store

{"error": "guard_missing", "detail": "WebSpec-Guard header required"}
```

At level 1, the shim's signed read, then the same bytes sent again:

```text
GET /note?id=welcome
Host: notes.localhost
WebSpec-Nonce: d0e6166b8bc1156ea3a28936479c4008
WebSpec-Guard: 205d2934

→ 200 OK
Cache-Control: no-store
WebSpec-Level: 1
WebSpec-Tier: open

{"result": "Hello from WebSpec."}
```

```text
GET /note?id=welcome
Host: notes.localhost
WebSpec-Nonce: d0e6166b8bc1156ea3a28936479c4008
WebSpec-Guard: 205d2934

→ 403 Forbidden
Cache-Control: no-store

{"error": "nonce_reused", "detail": "Nonce already used (single-use)"}
```

At level 3, a send whose harness policy declined to vouch for it, so it carries no clearance:

```text
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND
Idempotency-Key: 36686546-aef6-4a62-8935-d921a1e7adaf
WebSpec-Nonce: 9f47efa2acbb545e0027d2a51113151d
WebSpec-Guard: 2fd1b8ab

{"id":"welcome","to":"ana@example.com"}

→ 403 Forbidden
Cache-Control: no-store

{"error": "clearance_missing", "detail": "Level 3 requires a valid, unspent WebSpec-Clearance token for POST send_email on notes"}
```

## What to check in code

**Shims and harnesses**

- Header names are `WebSpec-Nonce`, `WebSpec-Guard`, `WebSpec-Clearance`, and
  `WebSpec-Approval`; `X-WebSpec-*` and `X-UFO-*` names are findings.
- A fresh nonce for every request, from `GET /__nonce` on the same destination (GD-4, HG-6).
- The guard message signs the path with its slashes trimmed, the canonical query, and the raw
  definer value (GD-2, GD-3). A tag computed over the untrimmed path or the raw query fails.
- The clearance binds the tool from the route map entry (`OPTIONS /`), not the path: on `POST`
  the definer selects the tool (CL-1).
- `Idempotency-Key` on every unsafe request to a non-idempotent tool from level 2, reused only
  for a retry of the same request (ID-1 to ID-3).
- A `428` retry sends the same method, path, arguments, and body, with a fresh nonce and guard
  and a fresh clearance (AP-3).
- Keys stay out of the model's context, logs, and prompts (SH-1).

**Deployment**

- The guard key comes from a secret store or `WEBSPEC_GUARD_KEY_FILE`, never from the repo or
  the config file (GD-5). `WEBSPEC_GUARD_KEY_DEV_EPHEMERAL` is for local development only.
- The gateway runs as its own OS user, not the agent's (DP-1).
- `WEBSPEC_APPROVERS_FILE` holds only public keys (AP-3).

## Known limits to keep in mind

Do not report these as implementation bugs; they are documented limits of the spec or the
reference gateway: one symmetric key behind levels 1 to 3; 32-bit guard and clearance tags; a
failed guard check does not consume its nonce; memory-only nonces, idempotency records, and
pins (a restart forgets them); and no check that a call matches what the user asked for,
except a person's review at level 4.

## Not part of WebSpec

Earlier drafts of this plugin described OAuth flows with PKCE, JWT session tokens, keychain
discovery, and device binding. None of these is in the spec. An MCP server may use OAuth
toward its own upstream service, but WebSpec does not specify it; do not report its absence
as a WebSpec finding.
