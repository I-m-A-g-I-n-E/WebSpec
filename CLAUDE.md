# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

WebSpec monorepo (`mono` branch) — protocol specification + infrastructure for tool invocation that maps to web primitives (DNS, subdomains, HTTP methods, paths, browser security). Everything lives here: the gateway, MCP services, Claude Code plugins, specification docs, and wiki.

## Monorepo Layout

- **gateway/** — Starlette REST-to-MCP bridge serving `*.i-a-m.live` via Cloudflare tunnel (port 7001)
- **services/mail-proton/** — FastMCP server wrapping Protonmail Bridge SMTP (port 1025)
- **services/op-auth/** — FastMCP server wrapping 1Password CLI (`op`) for per-secret access (guard-protected)
- **plugins/protonmail/** — Claude Code plugin: email skill
- **plugins/webspec-red-team/** — Claude Code plugin: 10 red-team pentesting agents
- **webspector/** — Claude Code plugin: WebSpec protocol validator (5 agents, 5 skills, 2 commands)
- **docs/** — WebSpec specification (MkDocs Material → webspec.gimme.tools)
- **wiki/** — Mirrored specification docs
- **apple_proposal/** — Apple-specific proposal documents

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
- **guard.py** — Session-key HMAC authentication + audience-bound single-use nonces. Services opt in with `"guard": true` in config. `/__nonce` endpoint for nonce bootstrap. Also: UFO clearance token computation (`compute_clearance_token`), provenance chain validation (`validate_provenance_chain`), `/__challenge` endpoint for dangerous-tier human confirmation.
- **handlers.py** — HTTP method → MCP tool dispatch. HEAD=ping, OPTIONS=schema, GET=read, POST/PUT/PATCH=mutation with definer validation
- **pool.py** — Lazy FastMCP client pool with per-service locks, 5-min tool cache TTL, 30s timeout
- **definer.py** — Tier 1 (verb header) and Tier 2 (HMAC bookend) validation for mutations. Verb families: POST→CREATE/SEND/INVOKE/TRIGGER/UPLOAD, PUT→REPLACE/OVERWRITE/SET, PATCH→MODIFY/APPEND/AMEND/RENAME
- **permissions.py** — fnmatch-based `METHOD:host/path` scope patterns. Currently `LOCAL_ALLOW_ALL`.
- **serializers.py** — MCP `CallToolResult` → JSON HTTP response

Tool name resolution: path segments use slash-to-underscore fallback (`/send/email` → `send_email`).

## Running the Gateway Locally

```bash
cd ~/MCP/webspec-gateway
WEBSPEC_DOMAIN=i-a-m.live WEBSPEC_PORT=7001 python -m webspec
```

Or via systemd: `systemctl --user start webspec-gateway`

Environment variables: `WEBSPEC_PORT` (default 7001), `WEBSPEC_HOST` (default 0.0.0.0), `WEBSPEC_DOMAIN` (public domain for Host routing), `WEBSPEC_LOG_LEVEL` (default info).

## MCP Services

**mail-proton**: FastMCP server exposing `send_email`, `list_senders`, `check_bridge` tools. Reads `PROTON_BRIDGE_PASSWORD` from env (passed via `~/.claude.json` mcpServers env block). Only two sender addresses allowed: `autodeveloper@pm.me`, `AIUnderstands@pm.me`. Requires Protonmail Bridge running (`/usr/lib/protonmail/bridge/bridge --grpc`).

**op-auth**: FastMCP server wrapping `op` CLI. Guard-protected (`"guard": true`). Tools: `read(reference)`, `list_vaults()`, `list_items(vault)`, `get_item(vault, item)`, `run(subcommand, args)`. UFO policy (`services/op-auth/ufo.py`): tools classified as open/sensitive/dangerous. `run()` uses strict allowlist (vault, item list/get, document get). All calls logged to `~/.webspec/op-auth-audit.jsonl`. Sensitive tools require `X-UFO-Clearance` header; dangerous tools require human confirmation via `/__challenge`. Reads `OP_SERVICE_ACCOUNT_TOKEN` from env.

## Building Docs

```bash
pip install mkdocs-material
mkdocs serve        # local preview
mkdocs gh-deploy    # push to GitHub Pages
```

CI deploys on push to `main` when docs/, mkdocs.yml, or requirements.txt change.

## Marketplace

`.claude-plugin/marketplace.json` at repo root registers three plugins: webspector, webspec-red-team, protonmail. Each plugin has its own `.claude-plugin/plugin.json`.

## FastMCP Quirk

FastMCP v2.14.5 constructor uses `instructions=` not `description=` — using `description=` throws TypeError.

## Secrets — Never Commit

- Bridge password comes from `PROTON_BRIDGE_PASSWORD` env var
- `~/.env` contains API keys and bridge password
- `~/.config/proton/proton.txt` has bridge IMAP/SMTP credentials
- `.gitignore` already excludes `.env` and `.env.*`
