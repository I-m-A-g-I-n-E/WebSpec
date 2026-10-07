# Human approval

At level 4, a person signs every destructive call, every `dangerous` call, and every call that
changes something in the open world, before it runs ([AP-1 to
AP-7](../spec/levels.md#level-4-witnessed)). The gateway holds only the approvers' *public*
keys. An agent harness that holds every other key, and is fully compromised, still cannot make
those calls on its own.

## 1. Make an approver key

For a quick test, a key on disk is enough:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/webspec-approver -C ana@example.com
```

For real use, keep the private half in an agent that asks for Touch ID or another gesture
before it signs. The 1Password SSH agent and keys backed by the Secure Enclave both do this.
Set the agent to ask as often as your risk calls for. Save the public key as
`~/.ssh/webspec-approver.pub`. `ssh-keygen -Y sign` signs with an agent-held key when it is
given the public key file.

## 2. Tell the gateway who may approve

List approvers in an OpenSSH *allowed signers* file, and restrict each entry to the
`webspec-approval` namespace:

```text
ana@example.com namespaces="webspec-approval" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA…
```

Point the gateway at the file with `WEBSPEC_APPROVERS_FILE=/etc/webspec/allowed_signers`.
Anyone who can add a line to this file can approve, so the agent must not be able to write it
([DP-4](../spec/audit-deployment.md#deployment)). If the variable is unset, the file names
nobody, or there is no `ssh-keygen` to verify with, calls that need approval are refused with
`503 approval_unavailable`, and no challenge is issued. The reference installers create the file with no approver in it, so level 4 starts working only once
you add a line.

## 3. Raise the destination to level 4

```json
{"mcpServers": {"notes": {"command": "python", "args": ["gateway/examples/demo_server.py"],
                          "level": 4}}}
```

## 4. Approve a request

When a call needs approval, the gateway answers `428 Precondition Required`. The body is a
challenge that describes exactly that request ([walkthrough](walkthrough.md#level-4-witnessed)).
The harness hands the challenge to a person, for example by saving it as `challenge.json`.
The person runs the following. The challenge here is the one from the walkthrough:

```console
$ webspec-ctl approve challenge.json --key ~/.ssh/webspec-approver.pub

=== WebSpec level-4 approval request ===
{
  "args": {
    "id": "welcome",
    "to": "ana@example.com"
  },
  "body_sha256": "00f8f87d43343409d8bd484d75f05317569f4d37b7af69bfc41940b22aa12075",
  "host": "notes.localhost",
  "method": "POST",
  "path": "/send_note",
  "service": "notes",
  "tool": "send_note",
  "v": 1
}
fingerprint: 4f935160084ad6289b3eb2b1216fd7079f09e6bddc8ea822d4507299599dbb84

Type 'approve' to sign this exact request: approve
X-WebSpec-Approval: 053f02f8b18f3e5c0a8eb7762777f8be:U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAg…
```

Before it asks anything, `webspec-ctl approve` does three things:

- It recomputes the fingerprint from the summary it is about to show, and refuses a challenge
  whose fingerprint doesn't match. What you see is what you sign.
- It escapes every control and non-ASCII character in the summary, so ANSI escapes,
  right-to-left overrides, and look-alike characters in the arguments can't disguise the
  request.
- It reads `approve` from the controlling terminal, never from stdin, so an agent's
  non-interactive shell can't answer for you.

The key can also come from `WEBSPEC_APPROVER_KEY`. A different signer that accepts
`ssh-keygen -Y sign` arguments can be chosen with `--signer` or `WEBSPEC_APPROVAL_SIGNER`.

## 5. Retry

Within 300 seconds, the harness retries the same request: the same method, path, arguments,
and body, with a fresh nonce and guard, a clearance minted within the last 30 seconds, and the
printed header. The reference shim does all of this. Reuse the same `Idempotency-Key` so that
the retry is the same operation:

```python
note = {"id": "welcome", "to": "ana@example.com"}
first = notes.call("POST", "send_note", note, "SEND", idempotency_key="k-91c2")    # 428
header = "…"   # the X-WebSpec-Approval value from webspec-ctl approve
notes.call("POST", "send_note", note, "SEND", approval=header, idempotency_key="k-91c2")  # 200
```

The signature is safe to hand back to the agent, because it approves only that one request,
once.

## When it fails

| Answer | Why |
|---|---|
| `403 approval_mismatch` | The retry is not identical to the request that was approved |
| `403 approval_reused` | The approval was already spent |
| `403 approval_invalid` | The signature isn't from a listed key, in the `webspec-approval` namespace. Five failures burn the challenge |
| `403 approval_expired` / `approval_unknown` | More than 300 seconds have passed, or the challenge was burned. Start over |
| `403 approval_malformed` | The header isn't `<challenge>:<base64 signature>` |
| `403 approval_in_progress` | The same challenge is being verified by another request. Retry in a moment |
| `429 approval_queue_full` | 100 challenges were issued for this destination in the last 300 seconds. Try again later |
| `503 approval_unavailable` | No approvers are configured |

A retry of the same request that carries no approval gets the same pending challenge back, so
an impatient agent can't flood the queue with copies of one request.

## What level 4 does and doesn't do

Level 4 holds against a fully compromised harness for the calls that it witnesses. Reads, and
changes that stay inside your own systems, follow the lower levels' rules. Its weak point is
the person: someone who approves a prompt they didn't expect defeats it. Level 4 asks only
for destructive, dangerous, and open-world changes, so prompts stay rare enough to read.
