# Isolation Model

WebSpec uses subdomains as natural security boundaries, inheriting decades of browser security engineering.

## The Same-Origin Policy Gift

Browsers enforce strict isolation between origins. An origin is defined as:

```
scheme + host + port
[https://slack.gimme.tools:443](https://slack.gimme.tools:443)
```

Different subdomains = different origins = **isolated by default**.

```
[slack.gimme.tools](http://slack.gimme.tools)    ↔    [notion.gimme.tools](http://notion.gimme.tools)
        │                          │
        │    BROWSER BLOCKS        │
        │    cross-origin access   │
        │                          │
   localStorage              localStorage
   cookies                   cookies
   IndexedDB                 IndexedDB
   (isolated)                (isolated)
```

A compromised or malicious service integration **cannot** read tokens or data from another service. We didn't write this code—decades of browser security did.

---

## Architecture Overview

```
                 [gimme.tools](http://gimme.tools) (apex)
                      │
     ┌────────────────┼────────────────┐
     │                │                │
[auth.gimme.tools](http://auth.gimme.tools)  [api.gimme.tools](http://api.gimme.tools)  {service}.[gimme.tools](http://gimme.tools)
     │                │                │
issues tokens     validates &      isolated service
(HttpOnly)        routes           origins
```

### Special Subdomains

| Subdomain | Role |
| --- | --- |
| [`auth.gimme.tools`](http://auth.gimme.tools) | Issues and validates tokens. Only place session cookies live. |
| [`api.gimme.tools`](http://api.gimme.tools) | Gateway. Routes requests, validates tokens, enforces scopes. |
| [`local.gimme.tools`](http://local.gimme.tools) | Bridge to [localhost:7001](http://localhost:7001). Tunnels to user's machine. |
| `{service}.[gimme.tools](http://gimme.tools)` | Service integrations. Isolated origins. |

---

## What Services CANNOT Do

Because each service is a separate origin:

| Attack | Prevented By |
| --- | --- |
| Read another service's localStorage | Same-origin policy |
| Access another service's cookies | Cookie scoping |
| Intercept another service's requests | Origin isolation |
| Steal session tokens | HttpOnly + auth subdomain |
| Impersonate another service | Audience-bound tokens |

---

## SSL/TLS Considerations

### Wildcard Certificate Scope

A `*.[gimme.tools](http://gimme.tools)` certificate covers:

```
✅ [slack.gimme.tools](http://slack.gimme.tools)
✅ [notion.gimme.tools](http://notion.gimme.tools)
✅ [api.gimme.tools](http://api.gimme.tools)
❌ [api.slack.gimme.tools](http://api.slack.gimme.tools)    ← Two levels deep, needs separate cert
```

This constraint **is a feature**: it enforces a flat subdomain structure.

### The Architecture This Forces

```
# Good: services are subdomains, tools are paths
[https://slack.gimme.tools/message](https://slack.gimme.tools/message)
[https://slack.gimme.tools/file](https://slack.gimme.tools/file)

# Bad: tools as subdomains (breaks wildcard)
[https://message.slack.gimme.tools](https://message.slack.gimme.tools)
[https://file.slack.gimme.tools](https://file.slack.gimme.tools)
```

---

## Trust Boundaries

```
┌─────────────────────────────────────────────────────────────┐
│                      TRUSTED CORE                            │
│  ┌──────────────────┐  ┌──────────────────┐                 │
│  │ [auth.gimme.tools](http://auth.gimme.tools) │  │ [api.gimme.tools](http://api.gimme.tools)  │                 │
│  │                  │  │                  │                 │
│  │ - Session mgmt   │  │ - Token verify   │                 │
│  │ - Token issuance │  │ - Scope enforce  │                 │
│  │ - OAuth handling │  │ - Request route  │                 │
│  └──────────────────┘  └──────────────────┘                 │
└─────────────────────────────────────────────────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          │                 │                 │
┌─────────┴──────┐  ┌─────┴────────────┐  ┌─┴───────────────┐
│ ISOLATED ORIGIN  │  │ ISOLATED ORIGIN  │  │ ISOLATED ORIGIN  │
│                  │  │                  │  │                  │
│ [slack.gimme.tools](http://slack.gimme.tools)│  │[notion.gimme.tools](http://notion.gimme.tools)│  │ [local.gimme.tools](http://local.gimme.tools)│
│                  │  │                  │  │                  │
│ - Scoped token   │  │ - Scoped token   │  │ - Scoped token   │
│ - Limited scope  │  │ - Limited scope  │  │ - Bridges to 7001│
│ - Can't see other│  │ - Can't see other│  │ - Can't see other│
└──────────────────┘  └──────────────────┘  └──────────────────┘
```

---

## Public Suffix List Option

For maximum isolation, [`gimme.tools`](http://gimme.tools) could be added to the Public Suffix List (like [`github.io`](http://github.io), [`vercel.app`](http://vercel.app)).

Effects:

| Feature | Normal | Public Suffix |
| --- | --- | --- |
| Cookie scope | Can span subdomains | Blocked entirely |
| Site isolation | Same site | Different sites |
| Credential sharing | Possible | Blocked |

Tradeoff: More friction for legitimate cross-service features. But all coordination should go through [`api.gimme.tools`](http://api.gimme.tools) anyway.

---

## Why This Matters

Every security boundary we inherit is:

- Code we don't write
- Bugs we don't create
- Audits we don't need
- Trust we don't have to build

The browser does the heavy lifting. We just structure our subdomains correctly.