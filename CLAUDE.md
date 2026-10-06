# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

WebSpec monorepo — protocol specification + reference implementation for tool invocation that maps to web primitives (DNS, subdomains, HTTP methods, paths, browser security). Everything lives here: the gateway, MCP services, Claude Code plugins, and the specification site.

## Monorepo Layout

- **gateway/** — Starlette REST-to-MCP bridge serving `*.i-a-m.live` via Cloudflare tunnel (port 7001)
- **services/mail-proton/** — FastMCP server wrapping Protonmail Bridge SMTP (port 1025)
- **services/op-auth/** — FastMCP server wrapping 1Password CLI (`op`) for per-secret access (guard-protected)
- **plugins/protonmail/** — Claude Code plugin: email skill
- **plugins/webspec-red-team/** — Claude Code plugin: 10 red-team pentesting agents
- **plugins/webspector/** — Claude Code plugin: WebSpec protocol validator (5 agents, 5 skills, 2 commands; predates the rewritten spec)
- **docs/** — The published spec site (MkDocs Material → https://i-m-a-g-i-n-e.github.io/WebSpec/). `docs/spec/` is normative and every requirement has a rule ID (`MB-1`, `GD-2`, …); `docs/guide/` is how-to and rationale. Long-form essays, the tier-C vision, retired designs, and article drafts live in Notion, not here.
- **gateway/examples/** — Demo MCP server, reference harness shim (stdlib only, written from the spec), and the walkthrough generator whose output is `docs/guide/walkthrough.md` (`tests/test_examples.py` replays it)
- **design/** — Design notes and implementation plans (not published)

## Live Infrastructure

The gateway runs as a systemd service. These symlinks exist on the host and **must not break**:

| Symlink | Target in monorepo |
|---|---|
| `~/MCP/webspec-gateway` | `gateway/` |
| `~/MCP/mail-proton` | `services/mail-proton/` |
| `~/MCP/op-auth` | `services/op-auth/` |
| `~/.claude/plugins/protonmail` | `plugins/protonmail/` |
| `~/.claude/plugins/webspec-red-team` | `plugins/webspec-red-team/` |

The systemd service (`webspec-gateway.service`) references `~/MCP/webspec-gateway` as its working directory. Renaming or restructuring `gateway/` will break the live service.

## Gateway Architecture

Traffic flow: `*.i-a-m.live` → Cloudflare tunnel → `localhost:7001` → gateway → MCP service

The gateway reads `~/.claude.json` `mcpServers` to discover services, normalizes names to subdomain labels, and routes by `Host` header. Key modules:

- **app.py** — Starlette Host() wildcard routing, config polling (30s), CORS, guard enforcement
- **config.py** — Parses `~/.claude.json`, `normalize_name()` for subdomain labels, `ServiceRegistry` with mtime-based reload. `guard` field on ServiceEntry.
- **guard.py** — Session-key HMAC authentication + audience-bound single-use nonces. Services opt in with `"guard": true` in config. `/__nonce` endpoint for nonce bootstrap. Also: UFO clearance token computation (`compute_clearance_token`), provenance chain validation (`validate_provenance_chain`), `/__challenge` is retired (410) — human confirmation is the level-4 approval flow.
- **handlers.py** — HTTP method → MCP tool dispatch under **method profiles** (docs/spec/methods.md, docs/spec/levels.md). HEAD/OPTIONS discover and never invoke; GET/POST/PUT/PATCH/DELETE invoke only through a method the tool's contract admits (else 405 + Allow), then enforce the level's requirements (definer/bookend, Idempotency-Key, UFO clearance, level-4 human approval)
- **methods.py** — Tool contracts (operator override > MCP ToolAnnotations > strict MCP defaults), contract pinning (join; servers can tighten, never loosen), method binding, per-method requirements by level 0–4
- **idempotency.py / approval.py / audit.py / hostgrammar.py / hardening.py** — Idempotency-Key store; level-4 Ed25519 approval verified with `ssh-keygen -Y verify` (`webspec-ctl approve` signs, using `WEBSPEC_APPROVER_KEY` / `WEBSPEC_APPROVAL_SIGNER`); hash-chained audit log (`WEBSPEC_AUDIT_LOG`); `{qualifier}*.{destination}.{domain}` host grammar; non-dumpable process on Linux
- **pool.py** — Lazy FastMCP client pool with per-service locks, 5-min tool cache TTL, 30s timeout
- **definer.py** — Tier 1 (verb header) and Tier 2 (HMAC bookend) validation for mutations. Verb families: POST→CREATE/SEND/INVOKE/TRIGGER/UPLOAD, PUT→REPLACE/OVERWRITE/SET, PATCH→MODIFY/APPEND/AMEND/RENAME, DELETE→REMOVE/REVOKE/ARCHIVE/CANCEL/PURGE
- **permissions.py** — fnmatch-based `METHOD:host/path` scope patterns. Currently `LOCAL_ALLOW_ALL`.
- **serializers.py** — MCP `CallToolResult` → JSON HTTP response

Tool name resolution: path segments use slash-to-underscore fallback (`/send/email` → `send_email`).

## Running the Gateway Locally

The gateway now **requires `WEBSPEC_GUARD_KEY`** in the environment and fails closed
(`GuardKeyError`) without it — the old `~/.webspec/session.key` file is obsolete and can
be deleted. Source the key from your password manager:

```bash
cd ~/MCP/webspec-gateway
WEBSPEC_GUARD_KEY=$(op read 'op://WebSpec/gateway-guard/key') \
  WEBSPEC_DOMAIN=i-a-m.live WEBSPEC_PORT=7001 python -m webspec
```

For throwaway local dev where you don't need a real guard key, use the ephemeral
escape hatch instead: `WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1 python -m webspec` (mints an
insecure in-memory key — do not use this on the public deployment).

Or via systemd: `systemctl --user start webspec-gateway`. The unit sources
`~/.webspec/gateway.env` (via `EnvironmentFile=-`) for `WEBSPEC_GUARD_KEY` — see
`gateway/systemd/webspec-gateway.service` for how to populate it from your vault.

Environment variables: `WEBSPEC_AUDIT_LOG` (audit chain path; empty disables), `WEBSPEC_APPROVERS_FILE` (ssh allowed-signers for level 4), `WEBSPEC_SSH_KEYGEN` (path of the `ssh-keygen` that verifies level-4 approvals; default from PATH), `WEBSPEC_ACCESS_LOG` (`1` enables uvicorn's access log — off by default because GET arguments live in URLs), `WEBSPEC_INTERNAL_PORT` (gateway listen port behind Caddy; falls back to `WEBSPEC_PORT`), `WEBSPEC_PORT` (default 7001), `WEBSPEC_HOST` (default 127.0.0.1), `WEBSPEC_DOMAIN` (public domain for Host routing), `WEBSPEC_LOG_LEVEL` (default info), `WEBSPEC_GUARD_KEY` (required — HMAC guard key, 64-hex or any passphrase), `WEBSPEC_GUARD_KEY_DEV_EPHEMERAL` (dev-only escape hatch, mints an insecure in-memory key).

## MCP Services

**mail-proton**: FastMCP server exposing `send_email`, `list_senders`, `check_bridge` tools. Reads `PROTON_BRIDGE_PASSWORD` from env (passed via `~/.claude.json` mcpServers env block). Only two sender addresses allowed: `autodeveloper@pm.me`, `AIUnderstands@pm.me`. Requires Protonmail Bridge running (`/usr/lib/protonmail/bridge/bridge --grpc`).

**op-auth**: FastMCP server wrapping `op` CLI. Guard-protected (`"guard": true`). Tools: `read(reference)`, `list_vaults()`, `list_items(vault)`, `get_item(vault, item)`, `run(subcommand, args)`. UFO policy (`services/op-auth/ufo.py`): tools classified as open/sensitive/dangerous. `run()` uses a strict allowlist (vault list/get, item list/get, document get; action must be the first argument; file-writing/config flags denied). All calls logged to `~/.webspec/op-auth-audit.jsonl`. Tools declare annotations + `webspec/tier` meta; at gateway level ≥ 3 sensitive tools require `X-UFO-Clearance`, and at level 4 dangerous tools require a human Ed25519 approval (428 challenge → `webspec-ctl approve`). Reads `OP_SERVICE_ACCOUNT_TOKEN` from env.

## Building Docs

```bash
pip install -r requirements.txt
mkdocs serve                     # local preview
mkdocs build --strict            # what CI runs
python -m pytest -q docs/tests   # rule IDs unique/resolvable, nav complete, code→docs links exist
```

`.github/workflows/deploy-docs.yml` builds and deploys to GitHub Pages (source: GitHub Actions) on push to `main` when docs/, mkdocs.yml, requirements.txt, or the workflow change. When gateway behavior changes, update the matching rule in `docs/spec/` and regenerate the walkthrough (`cd gateway && python examples/walkthrough.py`).

## Marketplace

`.claude-plugin/marketplace.json` at repo root registers three plugins: webspector, webspec-red-team, protonmail. Each plugin has its own `.claude-plugin/plugin.json`.

## FastMCP Quirk

FastMCP v2.14.5 constructor uses `instructions=` not `description=` — using `description=` throws TypeError.

## Secrets — Never Commit

- Bridge password comes from `PROTON_BRIDGE_PASSWORD` env var
- `~/.env` contains API keys and bridge password
- `~/.config/proton/proton.txt` has bridge IMAP/SMTP credentials
- `.gitignore` already excludes `.env` and `.env.*`
