# About this spec

The **Spec** tab is normative: it says what a WebSpec gateway must do. The **Guide** tab holds
walkthroughs, examples, and rationale, and is not normative. For the ideas behind the rules,
start with [How it works](../guide/how-it-works.md).

## Conventions

- MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are used as in RFC 2119 and RFC 8174.
- Every requirement has a stable ID, such as [MB-1](methods.md#method-binding). Each ID links
  to itself, so you can cite a rule by URL.
- Headers that begin `X-WebSpec-`, `X-Gimme-`, and `X-UFO-` are defined by this spec.
- Transcripts from the reference gateway use loopback names such as `notes.localhost`. Other
  examples use `example.com`.

## Rules by prefix

| Prefix | Covers | Page |
|---|---|---|
| `HG` `PA` `AR` | Host grammar, paths, arguments | [Addressing](addressing.md) |
| `TC` `MB` | Tool contracts and method binding | [Methods and contracts](methods.md) |
| `DF` `RQ` | Definers, and what each request must carry | [Methods and contracts](methods.md#per-method-rules) |
| `RS` `DS` | Responses and discovery | [Methods and contracts](methods.md#responses) |
| `LV` `PO` | Levels and processing order | [Security levels](levels.md) |
| `GD` `ID` `CL` `AP` | Guard (level 1), idempotency (2), clearance (3), approval (4) | [Security levels](levels.md#level-1-signed) |
| `CO` `SH` | Commit, and the harness shim | [Security levels](levels.md#commit) |
| `AU` `DP` | Audit log and deployment | [Audit and deployment](audit-deployment.md) |

## Conformance

A gateway conforms at level *n* when it meets every requirement that applies at levels up to
*n*. The [conformance table](status.md#conformance) lists which rules apply where, and
[Known limits](status.md#known-limits) lists where the reference gateway falls short.
