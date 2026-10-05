# UFO: Prompt Injection Mitigation for op-auth

**Date:** 2026-02-17
**Status:** Approved
**Scope:** op-auth service only (generalize in v2)

---

## Problem

The guard middleware (HMAC + nonce) authenticates *callers*, but doesn't protect against the **coerced agent** attack: an LLM tricked by prompt injection into using its own valid credentials to read secrets and exfiltrate them. The guard passes because the LLM *is* the legitimate caller.

## Naming: UFO (Unbounded File Object)

- Data ingested from external sources (documents, emails, tool responses) is **UFO-tagged**
- Tool calls carrying UFO arguments hit a **UFO quarantine** — held, not executed
- Execution requires **UFO clearance** — proof the call was initiated from clean context

## Design

### 1. Tool Sensitivity Tiers

Every op-auth tool is classified:

| Tier | Tools | Required Auth |
|------|-------|---------------|
| **open** | `list_vaults` | Guard (HMAC + nonce) |
| **sensitive** | `read`, `list_items`, `get_item` | Guard + UFO clearance |
| **dangerous** | `run` | Guard + UFO clearance + human confirmation |

Tiers are declared in the op-auth service itself via `ufo.py`, not the gateway. Policy stays close to the code that knows what each tool does.

### 2. UFO Clearance Tokens

**Clearance token** = `HMAC-SHA256(session_key, "ufo:" + tool_name + ":" + sorted_args_canonical + ":" + timestamp)` truncated to 8 hex chars.

Sent as header: `X-UFO-Clearance: <token>:<timestamp>`

Properties:
- Bound to the exact `(tool, arguments)` tuple — a token for `list_vaults` can't authorize `read`
- Timestamp prevents replay — tokens expire after 30 seconds
- Requires the session key (`~/.webspec/session.key`), which injected content doesn't have access to

The gateway computes and validates clearance tokens. The caller's tooling (helper script, Claude Code hook) is responsible for producing them.

### 3. Provenance Chain

Each link in the call chain carries:
- **why** — inherited from the level above (the commander's "what" becomes the subordinate's "why")
- **what** — the action this level is performing
- **provenance signature** — cryptographic proof this link was authorized by the link above

A tool call only executes if the full provenance chain is intact back to a trusted origin. If any link was initiated by UFO data, the chain is broken and the call enters quarantine.

**Header format:**
```
X-UFO-Provenance: human:h3a9->agent:a7f2->gateway
```

Each link is a truncated HMAC proving the previous level authorized this level's action.

**Escalation model:**

| Suspicion | Trigger | Resolution |
|-----------|---------|------------|
| Low | `open` tier, clean chain | Auto-approve (guard suffices) |
| Medium | `sensitive` tier, clean chain | UFO clearance token required |
| High | `sensitive` tier, broken chain (UFO in ancestry) | Escalate up the chain; if agent can't clear, escalate to human |
| Critical | `dangerous` tier, any chain | Always escalate to human |

Escalation is natural, not binary. Most calls never reach the human. But the path is always there.

### 4. `run()` Allowlist

The `run()` tool switches from a blocklist to a strict allowlist:

**Allowed subcommands:** `vault`, `item list`, `item get`, `document get`

Everything else is denied. This is a much smaller surface than the current blocklist which only blocks 4 subcommands out of dozens.

### 5. Audit Log

All op-auth tool calls are logged to `~/.webspec/op-auth-audit.jsonl`:

```json
{
  "timestamp": "2026-02-17T12:00:00Z",
  "tool": "read",
  "args": {"reference": "op://Vault/Item/field"},
  "tier": "sensitive",
  "provenance": "human:h3a9->agent:a7f2->gateway",
  "outcome": "allowed",
  "clearance_valid": true
}
```

Append-only. Never truncated by the service. Denied calls are logged too.

## Implementation Shape

### New file: `services/op-auth/ufo.py`
- Tool tier classification dict
- `run()` allowlist
- Audit log writer (append to `~/.webspec/op-auth-audit.jsonl`)
- Clearance token validation helper

### Modified: `services/op-auth/server.py`
- Each tool checks its tier via `ufo.py` before executing
- `sensitive` tools require valid `X-UFO-Clearance` or refuse
- `dangerous` tools require clearance + human confirmation challenge
- `run()` uses allowlist instead of blocklist

### Modified: `gateway/webspec/guard.py`
- `compute_clearance_token(session_key, tool, args, timestamp)` — UFO clearance HMAC
- `validate_provenance_chain(header, session_key)` — walk chain, verify each link

### Modified: `gateway/webspec/app.py`
- After guard passes on guarded services: extract `X-UFO-Provenance` and `X-UFO-Clearance` headers
- Pass through to tool call as metadata
- `/__challenge` endpoint for dangerous-tier human confirmation

### Unchanged
`handlers.py`, `config.py`, `definer.py`, `pool.py`, `permissions.py` — UFO layers on top of existing guard without modifying core dispatch.

## Out of Scope (v2)

- Gateway-level UFO enforcement (services self-enforce for now)
- Automatic taint detection (relies on caller declaring provenance honestly)
- Multi-agent provenance chains (only human->agent->gateway for now)
- E2E encryption envelope (client encrypts reference with server pubkey)

## Attack Scenarios Addressed

| Attack | How UFO mitigates |
|--------|-------------------|
| Injected prompt says "read all secrets" | Agent can't produce clearance token for `read` — the token requires the session key, which the injection doesn't have |
| Injected prompt says "read session.key then read secrets" | Session key path is blocked from LLM file-reading tools |
| Agent coerced to call `run("item", ["delete", ...])` | `run()` allowlist denies `item delete` |
| Replay a captured clearance token | 30-second TTL + args binding prevents replay |
| Nonce from `list_vaults` used to authorize `read` | Clearance is bound to specific `(tool, args)` — not transferable |
| Slow exfiltration via `list_vaults` metadata | `list_vaults` is `open` tier — returns vault names only, no secrets. Acceptable risk. |
