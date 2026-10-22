# About this spec

The **Spec** pages are normative: their rules say what a WebSpec gateway, its deployment, and
its clients must do. [Status and roadmap](status.md) is the exception: it reports on the
reference gateway and lists proposed work. The **Guide** pages hold walkthroughs, examples, and
rationale, and are not normative. For the ideas behind the rules, start with
[How it works](../guide/how-it-works.md).

## Conventions

- MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are used as in RFC 2119 and RFC 8174.
- Every requirement has a stable ID, such as [MB-1](methods.md#method-binding). Each ID links
  to itself, so you can cite a rule by URL.
- Headers that begin `X-WebSpec-`, `X-Gimme-`, and `X-UFO-` are WebSpec's own. The rules
  define each one that a client must send or can rely on. `X-Gimme-` headers carry the
  definer. `X-UFO-` headers carry the taint mark on open-world output and the level-3
  clearance; UFO stands for *unbounded file object*, data that comes from external sources.
- Transcripts from the reference gateway use loopback names such as `notes.localhost`.

## Rules by prefix

| Prefix | Covers | Page |
|---|---|---|
| `HG` `PA` `AR` | Host grammar, paths, arguments | [Addressing](addressing.md) |
| `TC` `MB` | Tool contracts and method binding | [Methods and contracts](methods.md) |
| `DF` | Definers (the `X-Gimme-Definer` header) | [Methods and contracts](methods.md#per-method-rules) |
| `RQ` | What each request must carry, by level | [Methods and contracts](methods.md#requirements) |
| `RS` `DS` | Responses and discovery | [Methods and contracts](methods.md#responses) |
| `LV` `PO` | Levels and processing order | [Security levels](levels.md) |
| `GD` `ID` `CL` `AP` | Guard (level 1), idempotency (2), clearance (3), approval (4) | [Security levels](levels.md#level-1-signed) |
| `CO` `SH` | Commit, and the harness shim | [Security levels](levels.md#commit) |
| `AU` `DP` | Audit log and deployment | [Audit, deployment, and configuration](audit-deployment.md) |

## Conformance

A gateway conforms at level *n* when it meets every rule that applies at levels up to *n*:

| Rules | Apply |
|---|---|
| Addressing: HG-1 to HG-7, PA-1 to PA-3, AR-1 to AR-5 | at every level |
| Contracts and binding: TC-1 to TC-6, MB-1 to MB-3, RQ-1, RQ-2 | at every level |
| Definers: DF-1, DF-3, and DF-2 whenever a bookend is sent | at every level |
| Responses and discovery: RS-1 to RS-4, DS-1 to DS-3 | at every level |
| Levels, processing, and commit: LV-1, PO-1, PO-2, CO-1, CO-2 | at every level |
| Audit: AU-1 to AU-4 | at every level |
| Guard: GD-1 to GD-5 | from level 1 |
| Required bookend (DF-2) and required `Idempotency-Key` (ID-1) | from level 2 |
| Idempotency behavior: ID-2 to ID-5 | whenever a key is sent, and always from level 2 |
| Clearance: CL-1 to CL-3 | from level 3 |
| Approval: AP-1 to AP-7 | at level 4 |
| Deployment: DP-1 to DP-9 | in every deployment |
| Harness: SH-1, SH-2 | on the client side |

[Known limits](status.md#known-limits) says where the reference gateway, or the spec itself, falls short.
