# WebSpec Docker Compose stack

This directory contains a self-hosting stack for WebSpec: `caddy` (TLS-terminating
reverse proxy on the public port), `gateway` (the WebSpec REST-to-MCP bridge), and
`registry` (the service discovery/registry component).

## Quick start

```bash
cd docker
docker compose up --build
```

## Guard key sourcing

The gateway needs a guard key (`WEBSPEC_GUARD_KEY`) to authenticate guard-protected
services. You have two options:

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

If neither `WEBSPEC_GUARD_KEY` nor `OP_GUARD_KEY_REF` is set, the entrypoint does
nothing and simply execs the gateway — behaving exactly as it would outside Docker.

## Mounting the service config

The gateway reads its service registry from `WEBSPEC_CONFIG` (a `~/.claude.json`-shaped
file listing `mcpServers`). In the compose stack this is mounted read-only into the
`gateway` container at `/config/claude.json`.

By default the compose file mounts `./claude.json` (relative to the `docker/`
directory) — set `WEBSPEC_CONFIG_HOST` to point at a different host path, e.g.:

```bash
WEBSPEC_CONFIG_HOST=/home/me/.claude.json docker compose up --build
```

## Bring your own MCP service

WebSpec services can be either HTTP-based or stdio-based, and the two are handled
differently in a Dockerized deployment:

- **HTTP services** (services that speak MCP over HTTP) can join the compose network
  directly as additional services, following the same pattern as `gateway` and
  `registry`. Add them to `docker-compose.yml`, give them a service name, and reference
  that name (`http://<service>:<port>`) from the mounted `claude.json`.
- **stdio services** (e.g. `op-auth`, `mail-proton`) are spawned as local subprocesses
  by the gateway's MCP client pool, not reached over the network. These continue to run
  on the host (or in their own container with process-launch access), and the mounted
  `claude.json` simply points at the command used to start them, exactly as it does for
  a non-Dockerized gateway.

## The `registry` service

The `registry` service's command (`python -m webspec_registry`) refers to a package
that is delivered in a separate plan and is not part of this repository yet. The
compose file keeps the `registry` block defined — `docker compose config` validates it
as structurally correct YAML even though the image will fail to build/run until that
package lands. If you need a stack that boots today, comment out (or remove) the
`registry:` block; `caddy` and `gateway` do not depend on it.
