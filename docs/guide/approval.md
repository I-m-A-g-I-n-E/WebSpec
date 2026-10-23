# Human approval

At level 4, a person signs every destructive call, every call to a tool marked `dangerous`
(the highest [tier](annotating.md#raising-the-tier)), and every call that changes something in
the open world, before it runs ([RQ-1](../spec/methods.md#requirements),
[AP-1 to AP-7](../spec/levels.md#level-4-witnessed)). The gateway holds only the approvers'
*public* keys. An agent harness that holds every other key, and is fully compromised, still
cannot make those calls on its own, provided it cannot sign with an approver's key
([step 1](#1-make-an-approver-key)).

## 1. Make an approver key

For a quick test, a key on disk is enough. Without a passphrase, though, any process running
as you can sign approvals with it:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/webspec-approver -C ana@example.com
```

For real use, keep the private half in an agent that asks for Touch ID or another gesture
before it signs. The 1Password SSH agent and keys backed by the Secure Enclave both do this.
Set the agent to ask as often as it allows: the gateway checks only the signature, so any
process that gets one from the agent without a gesture, a compromised harness included, can
approve without `webspec-ctl`. Save the public key as `~/.ssh/webspec-approver.pub`. In step 4
you pass it to `webspec-ctl approve --key`, and the signer uses the matching private key in the
agent.

## 2. Tell the gateway who may approve

List approvers in an OpenSSH *allowed signers* file, and restrict each entry to the
`webspec-approval` namespace:

```text
ana@example.com namespaces="webspec-approval" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA…
```

The Linux and macOS installers create this file as `/etc/webspec/allowed_signers`, with no
approver in it, and point the gateway at it, so level 4 starts working once you add a line.
For a development run, start the gateway with `WEBSPEC_APPROVERS_FILE` set to your file's
path. The Docker image has neither the file nor `ssh-keygen`, so level 4 there needs a derived
image ([docker/README.md](https://github.com/I-m-A-g-I-n-E/WebSpec/blob/main/docker/README.md)).
Anyone who can add a line to this file can approve, so the agent must not be able to write it
([DP-4](../spec/audit-deployment.md#deployment)). Until the file names someone, calls that
need approval are refused ([When it fails](#when-it-fails)).

## 3. Raise the destination to level 4

Add `"level": 4` to the destination's entry: in `webspec.json` if you followed the
[Quickstart](index.md#2-point-the-gateway-at-an-mcp-server), or in `/etc/webspec/config.json`
on an installed host. The gateway picks up the change within 30 seconds.

```json
{
  "mcpServers": {
    "notes": {
      "command": "python",
      "args": ["gateway/examples/demo_server.py"],
      "level": 4
    }
  }
}
```

## 4. Approve a request

When a call needs approval, the gateway answers `428 Precondition Required`. The body is a
challenge that describes that request ([AP-1](../spec/levels.md#level-4-witnessed)
shows one in full). The harness hands the whole body to a person, for example by saving it as
`challenge.json`, and the person runs the following. The request here is the
[walkthrough's](walkthrough.md#level-4-witnessed) send:

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
  "path": "/email",
  "service": "notes",
  "tool": "send_email",
  "v": 1
}
fingerprint: a7f83b4e4407c38945721d25b3491eed7feb56b614a14484ec94a18b888782a1

Type 'approve' to sign this exact request: approve
WebSpec-Approval: 9921eec474816b9dff2590622f73af86:U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAg…
```

Before it signs, `webspec-ctl approve` does three things:

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

Within 300 seconds of the first `428`, retry the identical request with the printed header,
and reuse the same `Idempotency-Key` so that the retry is the same operation. The retry also
needs a fresh nonce and guard, and a clearance minted within the last 30 seconds
([AP-3](../spec/levels.md#level-4-witnessed)), which the reference shim adds. Here `notes` is
a new shim, made as in [Quickstart step 5](index.md#5-raise-the-security-level) once the
level is 4:

```python
note = {"id": "welcome", "to": "ana@example.com"}
first = notes.call("POST", "/email", note, "SEND", idempotency_key="k-91c2")    # 428
open("challenge.json", "w").write(first.text)   # for the person in step 4
header = "…"   # the WebSpec-Approval value from webspec-ctl approve
notes.call("POST", "/email", note, "SEND", approval=header, idempotency_key="k-91c2")  # 200
```

The signature is safe to hand back to the agent, because it approves only that one request,
once.

## When it fails

| Answer | Why |
|---|---|
| `403 approval_mismatch` | The retry is not identical to the request that was approved |
| `403 approval_reused` | The approval was already spent |
| `403 approval_invalid` | The signature isn't from a listed key, in the `webspec-approval` namespace. Five failures burn the challenge |
| `403 approval_expired` / `approval_unknown` | More than 300 seconds have passed, the challenge was burned, or the gateway restarted after issuing it. Start over |
| `403 approval_malformed` | The header isn't `<challenge>:<base64 signature>` |
| `403 approval_in_progress` | The same challenge is being verified by another request. Retry in a moment |
| `429 approval_queue_full` | 100 challenges were issued for this destination in the last 300 seconds. Try again later |
| `503 approval_unavailable` | `WEBSPEC_APPROVERS_FILE` is unset or names nobody, or there is no `ssh-keygen` to verify with. No challenge is issued |

A retry of the same request that carries no approval gets the same pending challenge back, so
an impatient agent can't flood the queue with copies of one request.

## Limits of level 4

Unless a tool is `dangerous`, two kinds of call follow the lower levels' rules: reads, and
changes that destroy nothing and stay inside your own systems. The reads include calls to
read-only open-world tools, such as a URL fetcher, which can carry data out in their arguments
([Known limits](../spec/status.md#known-limits)). To put a person in front of such a tool,
raise it to `dangerous` ([Raising the tier](annotating.md#raising-the-tier)).

Level 4's weak point is the person: someone who approves a prompt they didn't expect defeats
it. Prompts stay rare enough to read only if your tools are annotated. An unannotated tool
counts as destructive, so every call to it needs a person's signature
([What an unannotated tool costs](annotating.md#what-an-unannotated-tool-costs)).
