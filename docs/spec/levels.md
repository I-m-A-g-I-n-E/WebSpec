# Security levels

Each destination runs at a level from 0 to 4. Each level adds requirements to the ones below
it ([RQ-1](methods.md#requirements)), and none removes any. A harness shim, not the model, adds
what levels 1 to 4 require ([SH-1](#the-harness-shim)).

| Level | Name | Adds |
|---|---|---|
| 0 | local | Method binding ([MB-1](methods.md#method-binding)), definers ([DF-1](methods.md#per-method-rules)), strict arguments ([AR-1 to AR-5](addressing.md#arguments)). Loopback names only ([HG-7](addressing.md#host)) |
| 1 | signed | Guard HMAC over the method, host, path, query, body, definer, and idempotency key, with a single-use nonce bound to the destination |
| 2 | bound | `Idempotency-Key` for every non-idempotent tool, and a bookend on every unsafe method |
| 3 | cleared | Single-use clearance for every tool that is not read-only and every tool above tier `open` |
| 4 | witnessed | A person's signature over the request's method, destination, path, arguments, and body, for destructive tools, `dangerous` tools, and open-world mutations |

- **LV-1** A destination's level comes from its configuration key `level`. If `level` is
  absent, the level is 1 when `guard: true` is set and 0 otherwise. `guard: true` forces at
  least level 1, and every level of 1 or higher enables the guard. An invalid value MUST fail
  closed to level 4.

## Processing order

- **PO-1** A gateway SHOULD apply its checks in this order, so that a request is refused with
  the most basic applicable error. The reference gateway does:
    1. Host labels ([HG-2](addressing.md#host)), then repeated query keys ([AR-1](addressing.md#arguments))
    2. Qualifiers ([HG-3, HG-5](addressing.md#host))
    3. For a configured destination: public exposure ([HG-7](addressing.md#host)), then the guard
       ([GD-1](#level-1-signed)). `GET /__nonce` is answered here, and so is the retired
       `GET /__challenge` (`410 Gone`, [PA-2](addressing.md#path)).
    4. Destination (`404 unknown_service`), tool path (`400 missing_tool`), the server's tool list
       (`503 service_unavailable`), then the tool ([PA-1](addressing.md#path))
    5. Method binding ([MB-2](methods.md#method-binding))
    6. Method rules: empty `GET` body ([AR-2](addressing.md#arguments)), definer and bookend
       ([DF-1, DF-2](methods.md#per-method-rules))
    7. Arguments ([AR-3 to AR-5](addressing.md#arguments))
    8. `Idempotency-Key` present and well formed ([ID-1](#level-2-bound)), then a key seen before:
       a replay, `422`, or `409` ([ID-3](#level-2-bound))
    9. Clearance ([CL-1](#level-3-cleared)), then approval ([AP-7, AP-1](#level-4-witnessed))
    10. Commit ([CO-1](#commit)): a full store is detected here, and so is a clearance, approval,
        or key that another attempt took after it was checked. Then the call.

- **PO-2** Whatever its order, a gateway MUST NOT spend a clearance or an approval, or claim an
  idempotency key, before every check has passed ([CO-1](#commit)). A nonce, by contrast, is
  consumed as soon as its tag verifies, so every attempt needs a fresh one.

## Level 1: signed

- **GD-1** Every request to a destination at level 1 or higher MUST carry two headers.
  `X-WebSpec-Nonce` holds a nonce issued for that destination that is unused and less than 60 s
  old. `X-WebSpec-Guard` holds the request's HMAC tag. The errors are:

    | Status | Code |
    |---|---|
    | `401` | `guard_missing`, `nonce_missing` |
    | `403` | `guard_invalid`, `unknown_nonce`, `nonce_reused`, `nonce_expired`, `nonce_audience_mismatch` |

- **GD-2** The tag is the hex encoding of the first 4 bytes of `HMAC-SHA256(guard key, message)`,
  and it is compared without regard to case. The message is:

    ```
    METHOD ":" host ":" path ":" nonce ":" hex(sha256(body))
      [ ":?" canonical-query ]   ; only if the request has a query
      [ ":!" definer-header ]    ; only if X-Gimme-Definer is present
      [ ":#" idempotency-key ]   ; only if Idempotency-Key is present
    ```

    `host` is the `Host` header as the gateway receives it, port included. `path` is the decoded
    path with its surrounding slashes trimmed, prefixed by a single `/`. `definer-header` is the
    raw `X-Gimme-Definer` value, bookend included.

- **GD-3** The canonical query is built in three steps:
    1. Decode every pair as a form would (`+` is a space, percent-escapes are decoded as UTF-8),
       keeping blank values.
    2. Sort the pairs by key, then by value, comparing code points. Keys are unique
       ([AR-1](addressing.md#arguments)).
    3. Write each pair as `key=value` (a blank value gives `key=`), percent-encoding every
       character except the unreserved `A–Z a–z 0–9 - . _ ~` (UTF-8, so a space becomes `%20`),
       and join the pairs with `&`.

- **GD-4** A nonce is issued by `GET /__nonce`, signed as in GD-2 with an empty nonce, giving the
  message `GET:{host}:/__nonce::{hex(sha256(""))}`. The response is `{"nonce", "audience",
  "expires_at", "ttl_seconds"}`, where `expires_at` is in Unix seconds. A nonce is bound to its
  destination, valid for 60 s, and usable once.

- **GD-5** The guard key MUST be supplied from outside the gateway's code and configuration,
  and the gateway MUST refuse to serve requests that need the key when it is absent. The
  reference gateway needs the key for every unsafe request at every level, including level 0,
  because it validates definers with it. It reads the key from:
    1. `WEBSPEC_GUARD_KEY`: 64 hex digits, or a passphrase hashed with SHA-256, once the
       whitespace around it is trimmed. A value of only whitespace or invisible characters
       counts as unset.
    2. Failing that, the file that `WEBSPEC_GUARD_KEY_FILE` names, read on every request. A byte
       order mark, and whitespace or invisible format characters around the key, are trimmed.
       What remains must be one line of at least 16 characters, with no control or format
       character, and anything else is refused.

    Without a usable key, the gateway still starts, logs a warning, and answers those requests
    with a plain-text `500`, which it audits. With `WEBSPEC_REQUIRE_GUARD_KEY` set, as the Linux
    unit sets it, it does not start at all without a usable key in `WEBSPEC_GUARD_KEY` itself.

## Level 2: bound

From level 2, the definer of every unsafe request carries a bookend
([DF-2](methods.md#per-method-rules)), which adds nothing over the guard today
([Known limits](status.md#known-limits)).

- **ID-1** From level 2, every unsafe request to a tool that is not idempotent MUST carry an
  `Idempotency-Key` (`400 idempotency_key_required`). That includes a `DELETE`, because the
  tool's contract, not HTTP, decides whether a call is idempotent. The key is 1 to 255 visible
  ASCII characters (`400 idempotency_key_invalid`). Below level 2, a key is honored on any
  unsafe method that sends one.

- **ID-2** Keys are scoped per destination. A request's fingerprint is the SHA-256 of its
  method, path, canonical query, and body hash.

- **ID-3** When a key has been seen before, the gateway answers:

    | The same key was used for … | Response |
    |---|---|
    | the same fingerprint, and the call completed | the stored response with `Idempotent-Replayed: true`. The tool is **not** called again |
    | a different fingerprint | `422 idempotency_key_reused` |
    | the same request, which is still running | `409 idempotency_key_in_flight` |
    | a call that timed out, lost its connection, or failed after reaching the tool | `409 idempotency_outcome_unknown`. The tool may have run, so the client MUST check the outcome and then use a new key |
    | a call that completed with a result too large to keep (over 256 KiB) | `409 idempotency_result_not_replayable` |

- **ID-4** A replay is answered *before* the clearance and the approval are checked, so retrying
  a completed request needs no second clearance or human signature, as long as the gateway still
  holds its record ([ID-5](#level-2-bound)).

- **ID-5** The gateway MUST NOT evict in-flight records, or records whose outcome is unknown, to
  make room, because a forgotten record could let a side effect run twice. When only such
  records remain, it refuses new keys for that destination with `503 idempotency_store_full`.
  The reference store holds 2,000 keys per destination. When it is full, only *completed*
  records are evicted, oldest first, and a retry whose record was evicted runs the tool again.
  Settled records (completed, or with an unknown outcome) are kept for up to 24 hours, and an
  in-flight record older than 15 minutes is marked *outcome unknown*.

## Level 3: cleared

A clearance is the harness's policy vouching for one call, typically after it has checked that
untrusted data (a response marked `X-UFO-Taint`, [RS-2](methods.md#responses)) did not steer
the call. In the reference gateway it is keyed with the guard key, so it is a policy checkpoint,
not a separate lock ([Known limits](status.md#known-limits)).

- **CL-1** A request that needs clearance ([RQ-1](methods.md#requirements)) MUST carry
  `X-UFO-Clearance: tag:ts`. Here `ts` is Unix time in seconds, and `tag` is the hex encoding of
  the first 4 bytes of:

    ```
    HMAC-SHA256(key, "ufo2:" destination ":" METHOD ":" tool ":"
                     canonical-args ":" ts)
    ```

    `canonical-args` is the arguments object as the gateway derives it from the request (decoded
    query values merged with the body), serialized as *canonical JSON*. That means keys sorted,
    `,` and `:` as separators with no whitespace, every non-ASCII character escaped as `\uXXXX`,
    and numbers written as Python's `json` module writes them (for example `1e-07`). A client in
    another language must reproduce this serialization exactly.

- **CL-2** A clearance is valid for 30 s on either side of `ts` and can be spent once. The
  errors are `403 clearance_missing`, `clearance_malformed`, `clearance_expired`,
  `clearance_invalid`, and `clearance_reused`.

- **CL-3** The gateway checks a clearance early but spends it only at commit
  ([CO-1](#commit)). A request refused later, for example by a level-4 challenge, therefore does
  not burn the clearance that its approved retry needs.

## Level 4: witnessed

- **AP-1** A request that needs approval and has no `X-WebSpec-Approval` header gets `428
  Precondition Required` with a challenge:

    ``` json
    {
      "error": "approval_required",
      "detail": "This action requires human approval (level 4). …",
      "challenge": "053f02f8b18f3e5c0a8eb7762777f8be",
      "fingerprint": "4f935160084ad6289b3eb2b1216fd7079f09e6bddc8ea822d4507299599dbb84",
      "summary": {"v": 1, "method": "POST", "service": "notes", "host": "notes.localhost",
                  "path": "/send_note", "tool": "send_note",
                  "args": {"id": "welcome", "to": "ana@example.com"},
                  "body_sha256": "00f8f87d43343409d8bd484d75f05317569f4d37b7af69bfc41940b22aa12075"},
      "sign_message": "webspec-approval/v1 053f02f8b18f3e5c0a8eb7762777f8be 4f935160084ad6289b3eb2b1216fd7079f09e6bddc8ea822d4507299599dbb84",
      "namespace": "webspec-approval",
      "expires_in": 300
    }
    ```

    The `fingerprint` is the SHA-256 of the summary serialized as canonical JSON (as in
    [CL-1](#level-3-cleared)). It binds the challenge to this method, destination, host, path,
    tool, arguments, and body. It does not cover the definer verb or the idempotency key.

- **AP-2** An approval tool MUST do four things. It MUST re-derive the fingerprint from the
  summary it shows, and refuse on a mismatch (what you see is what you sign). It MUST show the
  summary with every control and non-ASCII character escaped. It MUST read the confirmation from
  the controlling terminal, never from stdin. And it MUST sign `sign_message` in the SSH
  signature namespace `webspec-approval`. The reference tool is `webspec-ctl approve`.

- **AP-3** The client retries the same method, path, arguments, and body, with a fresh nonce
  and guard, a clearance minted within the last 30 s, and
  `X-WebSpec-Approval: <challenge>:<base64 SSH signature>`. The gateway verifies the signature
  with OpenSSH (`ssh-keygen -Y verify`) against an allowed-signers file
  (`WEBSPEC_APPROVERS_FILE`), so it holds only the approvers' *public* keys.

- **AP-4** A challenge is valid for 300 s and is single-use. The gateway answers `403` when an
  approval:

    | Code | The approval … |
    |---|---|
    | `approval_mismatch` | belongs to a challenge for a different request |
    | `approval_reused` | was already spent |
    | `approval_invalid` | is not signed by a listed key, or differs from the signature already verified for this challenge |
    | `approval_unknown`, `approval_expired` | names a challenge that was never issued, was burned, or is too old |
    | `approval_malformed` | is not `<challenge>:<base64 SSH signature>` |
    | `approval_in_progress` | arrives while the same challenge is being verified |

    Five invalid signatures burn the challenge.

- **AP-5** Verifying a signature does not spend it. The approval is spent at commit
  ([CO-1](#commit)). A retry that carries the same signature is accepted again until then. A
  retry of the same request without a signature gets the same pending challenge back.

- **AP-6** Pending challenges MUST NOT be evicted to make room. Otherwise anyone holding the
  guard key could flush out the challenge a person is in the middle of signing. When the queue
  is full, new challenges are refused with `429 approval_queue_full`. In the reference gateway,
  the queue holds at most 100 challenges issued for the destination in the last 300 s, whatever
  their state.

- **AP-7** If no approvers are configured, a request that needs approval MUST be refused with
  `503 approval_unavailable`, before any challenge is issued.

[Human approval](../guide/approval.md) shows how to set up approvers and their keys, and what
level 4 does not cover.

## Commit

- **CO-1** The clearance and the approval are spent at commit, immediately before the call is
  attempted, and never earlier. Once every check has passed, the gateway runs these steps with
  nothing interleaved until the call is attempted:
    1. Confirm that the clearance and the approval are still unspent.
    2. Claim the idempotency key.
    3. Spend the clearance and the approval.

    If the claim fails because another attempt holds the key, nothing has been spent, and the
    approved retry still works. A failure *after* commit leaves the clearance and the approval
    spent, even when the tool did not run, so the retry needs new ones.

- **CO-2** After the call, the gateway settles the idempotency key:

    | Outcome | Key |
    |---|---|
    | A result is returned | stored for replay |
    | The server answered with a JSON-RPC error, so the tool did not run | released |
    | A timeout, a connection that closed during the call, or a failure after the call | marked *outcome unknown* |

## The harness shim

- **SH-1** The model SHOULD write only the method, host, path, arguments, and definer verb.
  Nonces, guard and bookend tags, idempotency keys, and clearances SHOULD be added by a harness
  component, the shim, that keeps its keys where the model cannot read them and takes no
  instructions from text.

- **SH-2** The shim SHOULD learn what to add from `OPTIONS /{tool}`
  ([DS-1](methods.md#discovery)).
