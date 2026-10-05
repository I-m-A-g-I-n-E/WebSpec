# WebSpec Docker Compose stack

This directory contains a self-hosting stack for WebSpec: `caddy` (reverse proxy on
the public port) and `gateway` (the WebSpec REST-to-MCP bridge). A `registry`
service (discovery component) is defined but commented out — see below.

The files in `docker/caddy/` (`Caddyfile`, `conf.d/`) and `docker/claude.json` ship
as working defaults, so a fresh checkout boots with no setup beyond providing a
guard key:

## Quick start

```bash
cd /path/to/WebSpec        # repo root
docker compose -f docker/docker-compose.yml up --build
```

This brings up `caddy` (listening on `localhost:7001`) and `gateway` (internal,
reached only through caddy). With the default `docker/claude.json` (`{"mcpServers":
{}}`) the gateway registers no services, but it boots and answers on port 7001 —
useful to confirm the stack itself works before wiring in real services.

## Guard key sourcing

The gateway **requires** a guard key (`WEBSPEC_GUARD_KEY`) to authenticate
guard-protected services; without one it fails closed (`GuardKeyError`) for any
guarded service. You have two options:

- **Set it directly**: export `WEBSPEC_GUARD_KEY` before running `docker compose up`.
  It is passed through to the `gateway` container as-is.
- **Source it from 1Password**: set `OP_GUARD_KEY_REF` to an `op://` secret reference
  (e.g. `op://vault/item/field`). `docker/entrypoint.sh` runs inside the container on
  startup and, if `WEBSPEC_GUARD_KEY` is not already set, shells out to `op read
  "$OP_GUARD_KEY_REF"` to populate it before exec'ing the gateway process. This requires
  the `op` CLI and a valid 1Password session/service account token to be available
  inside the container (mount or bake in credentials as appropriate for your
  deployment). If `OP_GUARD_KEY_REF` is set but `op` is not on `PATH`, the entrypoint
  fails fast with a clear error instead of booting an unguarded gateway.

If neither `WEBSPEC_GUARD_KEY` nor `OP_GUARD_KEY_REF` is set, the gateway still boots
(needed so it can serve a clear error rather than refusing to launch), but any
guard-protected service will fail closed until a key is provided.

## Mounting the service config

The gateway reads its service registry from `WEBSPEC_CONFIG` (a `~/.claude.json`-shaped
file listing `mcpServers`). In the compose stack this is mounted read-only into the
`gateway` container at `/config/claude.json`.

By default the compose file mounts `docker/claude.json` — a sample file
(`{"mcpServers": {}}`) that ships in this repo, so the mount is always a real file
rather than an empty directory Docker would otherwise auto-create. Edit
`docker/claude.json` directly to add your MCP services, or point at a different host
path via `WEBSPEC_CONFIG_HOST`:

```bash
WEBSPEC_CONFIG_HOST=/home/me/.claude.json docker compose -f docker/docker-compose.yml up --build
```

If the mounted path is ever a directory or contains malformed JSON, the gateway
degrades to an empty service registry (logs a warning) rather than crashing at
startup.

## Exposing the stack publicly

By default (`WEBSPEC_DOMAIN` unset) the gateway registers no public `Host()` route —
the stack only answers on `localhost:7001`. Set `WEBSPEC_DOMAIN` (e.g. in a `.env`
file or exported before `docker compose up`) to activate the public-guard invariant:
once set, unguarded services return 403 on the public domain, and only services with
`"guard": true` in `claude.json` are reachable from outside. Pair this with a real
`WEBSPEC_GUARD_KEY` and a TLS-terminating proxy in front of caddy (or extend the
Caddyfile with your own TLS site block) before exposing port 7001 to the internet.

## Bring your own MCP service

WebSpec services can be either HTTP-based or stdio-based, and the two are handled
differently in a Dockerized deployment:

- **HTTP services** (services that speak MCP over HTTP) can join the compose network
  directly as additional services, following the same pattern as `gateway`. Add them
  to `docker-compose.yml`, give them a service name, and reference that name
  (`http://<service>:<port>`) from the mounted `claude.json`.
- **stdio services** (e.g. `op-auth`, `mail-proton`) are spawned as local subprocesses
  by the gateway's MCP client pool, not reached over the network. These continue to run
  on the host (or in their own container with process-launch access), and the mounted
  `claude.json` simply points at the command used to start them, exactly as it does for
  a non-Dockerized gateway.

## The `registry` service

The `registry` service's command (`python -m webspec_registry`) refers to a package
that is delivered in a separate plan and is not part of this repository yet. Its block
in `docker-compose.yml` is commented out so `docker compose up` boots today with just
`caddy` and `gateway`. Uncomment it once `webspec_registry` lands.
