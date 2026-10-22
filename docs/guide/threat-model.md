# Threat model and rationale

This page treats the agent as the adversary: one that is confused, steered by a prompt
injection, or working against you. WebSpec trusts the operator who runs the gateway, the host it
runs on, the annotations of the servers the operator adds, and the stdio servers the gateway
starts ([known limits](../spec/status.md#known-limits)). [How it works](how-it-works.md)
introduces the terms this page uses.

## The design in five decisions

1. **The gateway decides, not the caller.** A rule attached to a method is worthless if the
   caller picks the method. So the tool's contract picks the admissible methods
   ([MB-1](../spec/methods.md#method-binding)), and the gateway remembers the strictest contract
   it has seen since it started ([TC-6](../spec/methods.md#tool-contracts)).
2. **Unannotated means strict, and invalid means strictest.** Each of these gaps fails closed:
    - missing annotations take MCP's strict defaults;
    - a malformed override becomes the strictest contract;
    - an invalid level becomes level 4;
    - missing approvers mean that calls needing approval are refused (`503`).
3. **Keys never enter the model's context.** The model writes the method, the host, the path, the
   arguments, and one verb. A shim adds the signatures ([SH-1](../spec/levels.md#the-harness-shim)).
   Text the model reads, including a prompt injection, cannot forge what the model never
   holds.
4. **Clearances and approvals are spent only when the call is attempted.** The gateway checks
   them early but spends them at commit, immediately before the call, in the same step that
   claims the idempotency key ([CO-1](../spec/levels.md#commit)). So a request the gateway
   refuses never spends the clearance or approval that its retry needs. A failure after commit
   does spend them. Nonces are different: every attempt needs a fresh one
   ([PO-2](../spec/levels.md#processing-order)).
5. **The gateway's own checks are not advisory.** Capable agents treat advisory boundaries as
   obstacles to route around, so the gateway enforces each of its checks itself. Two controls
   sit outside the gateway: the harness's policy behind a clearance, and confining the agent's
   egress to the gateway ([DP-3](../spec/audit-deployment.md#deployment)).

## Attacks and controls

Each row pairs a behavior with the control that answers it, and says how far that control gets.
Named incidents come from the vendors' and investigators' own reports (dates as published);
*General* marks a common attack pattern.

| Observed behavior | Incident | Control | Effect |
|---|---|---|---|
| A destructive or supply-chain step (modify a repo, open a PR, trigger a CI beacon) | OpenAI agents vs. Hugging Face, July 2026 (OpenAI technical report, Aug 26, 2026) | Destructive tools and open-world changes need a person's approval at level 4 ([RQ-1](../spec/methods.md#requirements)) | **Stops** at level 4 |
| Calling a mutating tool through a weaker method (`GET`) | The reference gateway itself, before October 2026 | Method binding (`405`) | **Stops**, if the tool's contract is accurate |
| Reading credentials from `/proc/self/environ` or process memory | HF dataset-server file read (July 2026); an early Claude Mythos snapshot (system card, April 2026) | A dedicated OS user and a non-dumpable process. Level-4 approvals are verified with public keys only ([AP-3](../spec/levels.md#level-4-witnessed)), so no secret the gateway holds can forge one | **Stops** reads by other users. On Linux, also same-user reads of the gateway's memory and environment; on macOS, stdio servers can still read the guard-key file. Everywhere, stdio servers can read each other's environment |
| Replaying or forging calls with stolen request material | General | Single-use nonce; HMAC over method, host, path, query, body, definer, and idempotency key | **Stops** replay from level 1. Tags are 32 bits, so forgery by guessing is limited only by request rate ([known limits](../spec/status.md#known-limits)) |
| Concealment: rewriting git history, spoofing tool-call logs, rewriting activity when challenged | Claude Mythos system card; METR and Redwood Research (Aug 2026); UK AI Security Institute incident report (Aug 2026) | The hash-chained gateway audit log as ground truth | **Detects**, but only if the log, or at least its latest hash, is shipped off the host ([known limits](../spec/status.md#known-limits)) |
| Splitting a task into innocuous-looking steps | GTG-1002 espionage via Claude Code (Anthropic, Nov 2025) | The audit sequence, plus a person's gate on the final destructive step | **Slows or detects**. Rules on single steps don't catch it |
| Indirect prompt injection through fetched content | Claude Code Auto Mode bypass (Embrace The Red, Aug 2026); ChatGPT Atlas | `X-UFO-Taint`, the header that marks open-world output as untrusted; clearance at level 3 | **Slows**. Only as good as the shim's taint policy |
| Weaponized *arguments* to an allowed tool (a malicious HDF5 file, template injection) | HF intrusion, July 2026 | Strict JSON bodies and unique query keys ([AR-1 to AR-5](../spec/addressing.md#arguments)) | **Shape only**. Schema and semantic validation are proposed |
| Reading a secret, then sending it out | General exfiltration pattern | Open-world changes need a person at level 4 | **Stops** at level 4 for open-world tools that change something. A read-only open-world tool (a fetcher) is a gap unless raised to `dangerous` |
| Command and control over an allowed channel (polling a public dataset; directory names used as a message board) | HF intrusion; the Artifactory "message board", May–July 2026 | Audit, plus rate limits at the edge | **Detects at best** |
| Reaching hosts *around* the gateway; using harvested third-party credentials directly | HF intrusion (Modal relay); UNCTAD relay workarounds, 2026 | The gateway as the only egress ([DP-3](../spec/audit-deployment.md#deployment)) | **Stops** only if the operator enforces it at the network layer. No reference deployment does; [Egress](deploy.md#egress-dp-3) shows how |

## What WebSpec does not do

Beyond the gaps in the table:

- **It doesn't judge intent.** A well-formed, properly signed call that does the wrong thing
  for the user passes levels 0 to 3. At level 4, a person judges the calls they are asked to
  sign. Nothing judges the others.
- **It doesn't contain a compromised harness below level 4.** The guard key signs everything
  that levels 1 to 3 require.

The reference gateway's own gaps are listed under [Known limits](../spec/status.md#known-limits).
