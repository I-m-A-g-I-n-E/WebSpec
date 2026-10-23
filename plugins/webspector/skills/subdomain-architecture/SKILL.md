---
name: Subdomain Architecture
description: This skill should be used when checking how WebSpec destinations map to hosts and origins - the destination label, qualifier labels, loopback names versus a public domain, per-destination state, the proxy in front of the gateway, and CORS (WEBSPEC_CORS_ORIGINS, preflights, the headers an allowed origin may send and read). Trigger phrases include "destination", "subdomain", "qualifier", "origin", "CORS", "preflight", "localhost destination", "public domain", "unguarded_public", "proxy forwarding".
version: 2.0.0
---

# WebSpec destinations, hosts, and origins

This skill follows the normative spec pages
[Addressing](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/addressing/#host) (rules `HG-1` to
`HG-7`) and [Audit, deployment, and configuration](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/audit-deployment/#deployment)
(rules `DP-5`, `DP-8`). Cite a rule by its ID in every finding. Where this summary and the spec
differ, the spec wins.

## One destination per host

```text
host = 0*4( qualifier "." ) destination "." domain
```

- **HG-1** The destination, the label immediately left of the domain, is the only label that
  selects a service. Contracts, levels, nonce and clearance audiences, idempotency scopes, and
  approval queues are all per destination. An unconfigured destination is
  `404 unknown_service`.
- **HG-2** Every label is a lowercase DNS label, else `404 invalid_host`.
- **HG-6** A nonce, clearance, approval, or idempotency key issued for one destination is never
  honored for another.

The reference gateway derives each destination from its MCP server's name in the config: it
drops bracketed parts, lowercases, turns runs of `_` or whitespace into `-`, drops other
characters, and trims hyphens, so `MCP_DOCKER` becomes `mcp-docker`. A name longer than 63
characters is not a valid label, and every request for it gets `404 invalid_host`.

## Qualifiers route; they never isolate

- **HG-3** A qualifier is in the destination's `labels` allow-list, appears once, follows the
  list's order, and a host carries at most four. Otherwise `404 unknown_qualifier`. The
  default list is empty.
- **HG-4** A qualifier may change where a request lands, never what it means (method, path,
  definer) or who asks (the signed credentials). Labels are public: no tenant or user identity.
- **HG-5** A gateway that cannot route a qualifier to its own backend refuses it with
  `404 qualifier_not_routable` rather than serving it from the default backend. The reference
  gateway has no per-qualifier backends, so it refuses every qualifier.

The web already ties a qualifier to its destination: a cookie set for `mail.example.com` is
also sent to `eu.mail.example.com`, every host under one registrable domain is same-site, and
hosts with qualifiers need per-destination wildcard certificates, which Certificate
Transparency logs publish. A qualifier therefore lives inside its destination's trust zone.
Isolation comes from the destination label, which is its own origin.

## Loopback names and the public domain

- **HG-7** A destination below level 1 is not served on a public domain
  (`403 unguarded_public`). It is served only under loopback names such as
  `notes.localhost`, which keep it on the machine only while DP-5 and DP-8 hold.
- **DP-5** A proxy in front of the gateway forwards only WebSpec hosts, refuses every other
  host, and preserves the `Host` header, which the guard signs. Loopback names are WebSpec
  hosts only on connections from the host itself: a loopback name that arrived through the
  public path would reach level-0 destinations without the guard. The reference proxy answers
  `421` to every host it refuses.

## Origins and CORS (DP-8)

The gateway binds to loopback unless `WEBSPEC_HOST` says otherwise. Unless
`WEBSPEC_CORS_ORIGINS` lists an origin:

- a page from another origin cannot read the gateway's responses;
- the gateway refuses every CORS preflight, so the page cannot send `PUT`, `PATCH`, or `DELETE`,
  or any request with a non-safelisted header;
- `POST` is the only unsafe method a page can send without a preflight, and a `POST` must carry
  `WebSpec-Definer`, a header that needs one. A form the page posts arrives without a definer
  and is refused with `400 missing_definer`;
- its plain `GET`s still reach read-only tools on level-0 destinations, though it cannot read
  the answers.

When `WEBSPEC_CORS_ORIGINS` lists an origin, that origin may send `WebSpec-Definer`,
`WebSpec-Guard`, `WebSpec-Nonce`, `WebSpec-Clearance`, `WebSpec-Approval`, `Idempotency-Key`,
and `Content-Type` (`Access-Control-Allow-Headers`), and may read `Allow`, `Link`,
`WebSpec-Level`, `WebSpec-Tier`, `WebSpec-Taint`, `WebSpec-Status`, `WebSpec-Tool-Count`,
`WebSpec-Service`, and `Idempotent-Replayed` (`Access-Control-Expose-Headers`). Without
`WebSpec-Taint` in that list, a harness in the origin could not see the taint mark.

## Real exchanges

These are real answers of the reference gateway (`gateway/`), with the demo server
(`gateway/examples/demo_server.py`) configured as the level-0 destination `notes`, abridged to
the headers that matter.

```text
GET /note?id=welcome
Host: mail.localhost

→ 404 Not Found
Cache-Control: no-store

{"error": "unknown_service", "detail": "Unknown service: mail", "available": ["notes"]}
```

```text
GET /note?id=welcome
Host: eu.notes.localhost

→ 404 Not Found
Cache-Control: no-store

{"error": "unknown_qualifier", "detail": "These qualifier labels are not allowed for this destination."}
```

```text
OPTIONS /email
Host: notes.localhost
Origin: https://evil.example
Access-Control-Request-Method: POST
Access-Control-Request-Headers: content-type, webspec-definer

→ 400 Bad Request
Cache-Control: no-store

Disallowed CORS origin
```

```text
POST /email
Host: notes.localhost
Origin: https://evil.example
Content-Type: application/x-www-form-urlencoded

id=welcome&to=eve%40example.com

→ 400 Bad Request
Cache-Control: no-store

{"error": "missing_definer", "detail": "POST requires the WebSpec-Definer header"}
```

## What to check

**Gateway and proxy configuration**

- Every destination reachable on the public domain has `level` 1 or higher (HG-7).
- The proxy forwards only `{destination}.{domain}` hosts, and loopback names only from local
  connections; it preserves `Host` (DP-5).
- `WEBSPEC_HOST` is unset, so the gateway binds to loopback (DP-8). A wider bind is a finding
  unless the gateway sits in a network of its own, as in the Docker stack.
- `WEBSPEC_CORS_ORIGINS` lists only origins that run a harness you trust; a wildcard or a
  reflected `Origin` is a finding.
- `labels` names only qualifiers that are safe to publish (HG-4).

**Clients and harnesses**

- Each destination has its own nonces, clearances, approvals, and idempotency keys; never
  reuse one across destinations (HG-6).
- A browser-based harness on an allowed origin reads `WebSpec-Taint` from the exposed headers
  and treats tainted bodies as data.

## Not part of WebSpec

Earlier drafts of this plugin described `auth.`, `api.`, and `local.` subdomains under a fixed
domain, JWT audience claims, cookie attributes, and a WebSocket bridge to local services. None
of these is in the spec, and their absence is not a finding. General cookie and session
hardening is still good practice, but cite it as such, not as a WebSpec rule.
