# Security levels

Each destination runs at a level from 0 to 4. Each level adds requirements to the ones below
it ([RQ-1](methods.md#requirements)), and none removes any.

| Level | Name | Adds |
|---|---|---|
| 0 | local | Method binding, definers, strict arguments. Loopback only |
| 1 | signed | Guard HMAC over the whole request, with a single-use nonce bound to the destination |
| 2 | bound | Bookend on every unsafe method, and `Idempotency-Key` for every non-idempotent tool |
| 3 | cleared | Single-use clearance for every tool that is not read-only and every tool above tier `open` |
| 4 | witnessed | A person's signature over the exact request, for destructive tools, `dangerous` tools, and open-world mutations |

- **LV-1** A destination's level comes from its configuration key `level`. If `level` is
  absent, the level is 1 when `guard: true` is set and 0 otherwise. `guard: true` forces at
  least level 1, and every level of 1 or higher enables the guard. An invalid value MUST fail
  closed to level 4.
A destination served on a public domain must be at level 1 or higher
([HG-7](addressing.md#host)).

## Processing order

- **PO-1** A gateway SHOULD apply its checks in this order, so that a request is refused with
  the most basic applicable error:
    1. Host grammar ([HG-2, HG-3, HG-5](addressing.md#host)) and repeated query keys ([AR-1](addressing.md#arguments))
    2. Public exposure ([HG-7](addressing.md#host)), then the guard ([GD-1](#level-1-signed))
    3. Destination, then tool ([PA-1](addressing.md#path))
    4. Method binding ([MB-2](methods.md#method-binding))
    5. Method rules: empty `GET` body, definer, bookend ([DF-1, DF-2](methods.md#per-method-rules))
    6. Arguments ([AR-2 to AR-5](addressing.md#arguments))
    7. Idempotency replay ([ID-3](#level-2-bound))
    8. Clearance ([CL-1](#level-3-cleared)), then approval ([AP-1](#level-4-witnessed))
    9. Commit ([CO-1](#commit)), then the call
- **PO-2** Whatever its order, a gateway MUST NOT spend a clearance or an approval, or claim an
  idempotency key, before every check has passed ([CO-1](#commit)). A nonce, by contrast, is
  consumed when the guard checks it, so every attempt needs a fresh one.

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
      [ ":!" definer-header ]    ; only if X-Gimme-Definer is present (raw value, bookend included)
      [ ":#" idempotency-key ]   ; only if Idempotency-Key is present
    ```

    `host` is the `Host` header as the gateway receives it, port included. `path` is the decoded
    path with its surrounding slashes trimmed, prefixed by a single `/`.
- **GD-3** The canonical query is built in three steps:
    1. Decode every pair, keeping blank values.
    2. Sort the pairs by key. Keys are unique ([AR-1](addressing.md#arguments)).
    3. Re-encode with RFC 3986 percent-encoding. The unreserved characters `A–Z a–z 0–9 - . _ ~`
       stay literal, and a space becomes `%20`.
- **GD-4** A nonce is issued by `GET /__nonce`, signed as in GD-2 with an empty nonce, giving the
  message `GET:{host}:/__nonce::{hex(sha256(""))}`. The response is `{"nonce", "audience",
  "expires_at", "ttl_seconds"}`, where `expires_at` is in Unix seconds. A nonce is bound to its
  destination, valid for 60 s, and usable once.
- **GD-5** The guard key MUST be supplied from outside the gateway's code and configuration. The
  gateway MUST refuse to serve requests that need the key when it is absent. The reference
  gateway reads `WEBSPEC_GUARD_KEY`, which is either 64 hex digits or a passphrase hashed with
  SHA-256, and is meant to be filled from a password manager when the gateway is launched.

## Level 2: bound

Every unsafe method carries a bookend in its definer ([DF-2](methods.md#per-method-rules)).
- **ID-1** Every unsafe request to a tool that is not idempotent MUST carry an
  `Idempotency-Key` (`400 idempotency_key_required`). That includes `DELETE`. The key is 1 to
  255 visible ASCII characters (`400 idempotency_key_invalid`). Below level 2, a key is honored
  on any unsafe method that sends one.
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

- **ID-4** A replay is answered *before* one-time credentials are checked, so a legitimate
  retry never needs a second clearance or a second human signature.
- **ID-5** The gateway MUST NOT evict in-flight or unknown records to make room, because a
  forgotten record could let a side effect run twice. When only such records remain, it refuses
  new keys for that destination with `503 idempotency_store_full`. Settled records are kept for
  24 hours. An in-flight record older than 15 minutes becomes unknown. The reference store holds
  2,000 keys per destination.

## Level 3: cleared

- **CL-1** A request that needs clearance ([RQ-1](methods.md#requirements)) MUST carry
  `X-UFO-Clearance: tag:ts`. Here `ts` is Unix time in seconds, and `tag` is the hex encoding of
  the first 4 bytes of:

    ```
    HMAC-SHA256(key, "ufo2:" destination ":" METHOD ":" tool ":" canonical-args ":" ts)
    ```

    `canonical-args` is the arguments object serialized as JSON, with keys sorted, no
    whitespace, and ASCII escapes.
- **CL-2** A clearance is valid for 30 s on either side of `ts` and can be spent once. The
  errors are `403 clearance_missing`, `clearance_malformed`, `clearance_expired`,
  `clearance_invalid`, and `clearance_reused`.
- **CL-3** The gateway checks a clearance early but spends it only at commit
  ([CO-1](#commit)). A request refused later, for example by a level-4 challenge, therefore does
  not burn the clearance that its approved retry needs.

A clearance is the point where the harness's policy vouches for a sensitive call, typically
after checking that untrusted (UFO-tagged) data did not steer it. In the reference gateway the
clearance is keyed with the guard key. It adds a policy checkpoint bound to the exact call, but
not a separate cryptographic boundary. Independent signers per level are
[proposed](status.md#roadmap).

## Level 4: witnessed

- **AP-1** A request that needs approval and has no `X-WebSpec-Approval` header gets `428
  Precondition Required` with a challenge:

    ```json
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

    The `fingerprint` is the SHA-256 of the summary serialized as canonical JSON. It binds the
    challenge to exactly this request.
- **AP-2** An approval tool MUST do four things. It MUST re-derive the fingerprint from the
  summary it shows, and refuse on a mismatch (what you see is what you sign). It MUST show the
  summary with every control and non-ASCII character escaped. It MUST read the confirmation from
  the controlling terminal, never from stdin. And it MUST sign `sign_message` in the SSH
  signature namespace `webspec-approval`. The reference tool is `webspec-ctl approve`.
- **AP-3** The client retries the identical request with
  `X-WebSpec-Approval: <challenge>:<base64 SSH signature>`. The gateway verifies the signature
  with OpenSSH (`ssh-keygen -Y verify`) against an allowed-signers file
  (`WEBSPEC_APPROVERS_FILE`). The gateway holds only *public* keys.
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
- **AP-6** Pending challenges MUST NOT be evicted to make room. A full queue, which in the
  reference gateway is 100 per destination, refuses new challenges with `429
  approval_queue_full`. Otherwise anyone holding the guard key could flush out the challenge a
  person is in the middle of signing.
- **AP-7** If no approvers are configured, requests that need approval MUST fail closed with
  `503 approval_unavailable`.

The signing key SHOULD live in an agent that demands a biometric gesture for every signature,
such as the 1Password SSH agent or a key backed by the Secure Enclave. The remaining risk is a
person who approves a prompt they did not initiate.

## Commit

- **CO-1** One-time credentials are never consumed unless the call runs. Once every check has
  passed, the gateway runs these steps with nothing interleaved until the tool call starts:
    1. Confirm that the clearance and the approval are still unspent.
    2. Claim the idempotency key.
    3. Spend the clearance and the approval.

    If the claim fails because another attempt holds the key, nothing has been spent, and the
    approved retry still works.
- **CO-2** After the call, the gateway settles the idempotency key:

    | Outcome | Key |
    |---|---|
    | A result is returned | stored for replay |
    | The server refused the call (a JSON-RPC error, so the tool did not run) | released |
    | Timeout, broken connection, or a failure after the call | marked *outcome unknown* |

## The harness shim

- **SH-1** The model SHOULD write only the method, host, path, arguments, and definer verb.
  Keys, nonces, tags, idempotency keys, and clearances SHOULD be added by a harness component
  whose secrets the model cannot read and whose behavior text cannot instruct.
- **SH-2** The shim SHOULD learn what to add from `OPTIONS /{tool}`
  ([DS-1](methods.md#discovery)).

This is what makes WebSpec easy for the AI and hard for an attacker. The model's request is the
same at every level. Only the shim's work, and the operator's choice of level, change.
