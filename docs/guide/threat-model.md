# Threat model and rationale

## The design in five decisions

1. **The gateway decides, not the caller.** A rule attached to a method is worthless if the
   caller picks the method. So the tool's contract picks the admissible methods
   ([MB-1](../spec/methods.md#method-binding)), and the gateway remembers the strictest contract
   it has seen ([TC-6](../spec/methods.md#tool-contracts)).
2. **Unannotated means strict, and invalid means strictest.** Every gap fails closed:
   - missing annotations take MCP's strict defaults;
   - a malformed override becomes the strictest contract;
   - an invalid level becomes level 4;
   - missing approvers mean that level-4 calls are refused.
3. **Keys never enter the model's context.** The model writes the method, the path, the
   arguments, and one verb. A shim adds the signatures ([SH-1](../spec/levels.md#the-harness-shim)).
   Text the model reads, including a prompt injection, cannot forge what the model never
   holds.
4. **One-time credentials are spent only when the call runs.** The gateway checks early,
   claims the idempotency key, and spends at commit ([CO-1](../spec/levels.md#commit)). Retries
   are safe, and a refusal never burns the credential that the next attempt needs.
5. **Every rule is mandatory.** Capable agents treat advisory boundaries as obstacles to route
   around. Nothing in WebSpec is a hint to the agent.

## What 2026 actually looked like

The incidents below are taken from the vendors' and investigators' own reports (dates as
published). They are why the rules exist. They also mark what the rules do **not** stop.

| Observed behavior | Incident | Control | Effect |
|---|---|---|---|
| A destructive or supply-chain step (modify a repo, open a PR, trigger a CI beacon) | OpenAI agents vs. Hugging Face, July 2026 (OpenAI technical report, Aug 26, 2026) | Destructive contract → `DELETE`/`PUT`, plus a person's approval at level 4 | **Stops** at level 4 |
| Calling a mutating tool through a weaker verb | WebSpec's own gateway before method profiles | Method binding (`405`) | **Stops** |
| Reading credentials from `/proc/self/environ` or process memory | HF dataset-server file read (July 2026); an early Claude Mythos snapshot (system card, April 2026) | A dedicated OS user and a non-dumpable process. Level 4 holds only public keys | **Stops** same-user reads of the gateway |
| Replaying or forging calls with stolen request material | General | Single-use nonce; HMAC over method, host, path, query, and body | **Stops** |
| Concealment: rewriting git history, spoofing tool-call logs, rewriting activity when challenged | Mythos system card; METR/Redwood (Aug 2026); UK AISI incident report (Aug 2026) | The hash-chained gateway audit log as ground truth | **Detects** (prevents only if the log is out of the agent's reach) |
| Splitting a task into innocuous-looking steps | GTG-1002 espionage via Claude Code (Anthropic, Nov 2025) | The audit sequence, plus a person's gate on the final destructive step | **Slows or detects**. Rules on single steps don't catch it |
| Indirect prompt injection through fetched content | Claude Code Auto Mode bypass (Embrace The Red, Aug 2026); ChatGPT Atlas | `X-UFO-Taint` on open-world output; clearance at level 3 | **Slows**. Only as good as the shim's taint policy |
| Weaponized *arguments* to an allowed tool (a malicious HDF5 file, template injection) | HF intrusion, July 2026 | Strict JSON bodies, typed `GET` arguments, unique keys | **Shape only**. Semantic validation is proposed |
| Reading a secret, then sending it out | General exfiltration pattern | Open-world changes need a person at level 4 | **Stops** at level 4 |
| Command and control over an allowed channel (polling a public dataset; directory names used as a message board) | HF intrusion; the Artifactory "message board", May–July 2026 | Audit, plus rate limits at the edge | **Detects at best** |
| Reaching hosts *around* the gateway; using harvested third-party credentials directly | HF intrusion (Modal relay); UNCTAD relay workarounds, 2026 | The gateway as the only egress ([DP-3](../spec/audit-deployment.md#deployment)) | **Stops only if enforced at the network layer** |

The common thread in these reports is that capable, persistent agents treat security
boundaries as obstacles to route around. So every control here is mandatory and fails closed,
and the [known limits](../spec/status.md#known-limits) are written down rather than left for
someone to discover.

## What WebSpec does not do

- **It doesn't judge intent.** A well-formed, properly signed call that does the wrong thing
  for the user passes levels 0 to 3. Only level 4, a person, judges meaning, and only for the
  calls it witnesses.
- **It doesn't validate meaning.** Arguments are checked for shape, not for whether they are
  safe for the tool.
- **It doesn't contain a compromised harness below level 4.** The guard key signs everything
  that levels 1 to 3 require.
- **It doesn't help if traffic can bypass it.** The gateway must be the only way out.

## WebSpec and MCP

The two complement each other:

| MCP provides | WebSpec adds |
|---|---|
| A protocol for discovering and calling tools on a server | A URL and an HTTP method for each tool, so the web's own machinery (TLS, origins, CORS, proxies, logs) can see and police each call |
| Tool annotations as *hints* | Annotations turned into an enforced *contract*, pinned so a server can't loosen it |
| Transports: stdio and Streamable HTTP | A gateway that speaks those transports to unchanged servers |
| Authorization for remote servers (OAuth) | Per-call authority that scales with risk: signatures, single-use clearances, and a person's approval |

MCP lets an agent call a tool. WebSpec decides which calls get through, and leaves a record
that can't be quietly rewritten.

## The Apple parallel

WebSpec arrived at roughly the same shape as Apple's platform security:

| Apple | WebSpec |
|---|---|
| Entitlements: signed, declarative capabilities bound to the code's identity | Tool contracts. Operator overrides are the grants, and pinning keeps them stable |
| App Sandbox (Seatbelt): deny by default | Unannotated means strict; method binding; isolation per destination |
| TCC consent, with Touch ID and the Secure Enclave proving the user is present | Level-4 approval, signed by a biometric-gated key |
| XPC privilege separation | The gateway and each MCP server as separate processes and OS users |
| Hardened Runtime (no task-port or debugger attach) | A non-dumpable gateway process |
| Private Cloud Compute's verifiable transparency log | The hash-chained audit log |

Entitlement keys are reverse-DNS names (`com.apple.security.network.client`). WebSpec hosts
are the same naming tree, read forward.
