# WebSpec Docker Compose stack

This directory holds a self-hosting stack for WebSpec: `caddy`, the only published port, and
`gateway`, the WebSpec REST-to-MCP bridge. A `registry` service (discovery) is defined but
commented out; see [The registry service](#the-registry-service-experimental).

The stack is hardened for the deployment rules DP-1 to DP-9 in
[docs/spec/audit-deployment.md](../docs/spec/audit-deployment.md):

| Rule | How the stack meets it |
|---|---|
| DP-1 dedicated user | The gateway runs as `webspec` (uid and gid 10001, no login shell) in its own container, never as root. Caddy runs as uid 10002. See [Trust boundary](#trust-boundary) for who else can reach the key. |
| DP-2 non-dumpable | PID 1 is the image's init, `docker/init.py`, which starts the gateway, forwards signals to it and reaps orphans. Both make themselves non-dumpable, so the stdio MCP servers the gateway runs, which share their user, cannot read their environment or memory. Compose sets `init: false`, so Docker's own init never takes PID 1; see [Child processes](#bring-your-own-mcp-service). A process started with `docker compose exec` is not covered; see [Guard key](#guard-key). |
| DP-3 egress | Not enforced by the stack. Confine the agent's network at the host, and allow it `127.0.0.1:7001` and `[::1]:7001`; see [Ports](#ports). |
| DP-4 configuration and audit log | The configuration directory is mounted read-only, both containers have read-only root filesystems, and the code is owned by root. The gateway's Python ignores user site-packages, so nothing in its writable HOME runs inside it. The audit log lives in the `webspec-state` volume. |
| DP-5 forwarded hosts | Caddy forwards `localhost`, `*.localhost` and, when `WEBSPEC_DOMAIN` is set, `*.$WEBSPEC_DOMAIN`, with the `Host` header untouched. A loopback host that came through the Cloudflare tunnel, and any other host, gets `421 Misdirected Request`. Caddy speaks HTTP/1.1 only. Port 7001 is published on the loopback addresses only, `127.0.0.1` and `[::1]`. |
| DP-6 logs | Caddy writes no access log and deletes the request (its URI and headers) from its error log. The gateway's access log is off. |
| DP-7 one process | One `gateway` container running one gateway process. Do not scale it. |
| DP-8 bind address | The gateway listens only on the compose network, which is IPv4-only; see [Ports](#ports). The stack sets no `WEBSPEC_CORS_ORIGINS`, so pages from other origins cannot read the gateway's answers. |
| DP-9 held ports | The init holds the gateway's port, 7002, for the container's life; see [Restarts](#restarts). Caddy's published port is Docker's, which lets it go whenever the `caddy` container is down; see [Ports](#ports). |

Both containers also drop every capability (Caddy keeps one; see the comment in
`docker-compose.yml`), run with `no-new-privileges`, and restart unless you stop them; see
[Restarts](#restarts).

## Quick start

```bash
cd /path/to/WebSpec        # repo root
export WEBSPEC_GUARD_KEY=$(op read 'op://WebSpec/gateway-guard/key')   # or: openssl rand -hex 32
docker compose -f docker/docker-compose.yml up --build -d
curl -s http://localhost:7001/
# {"services":[]}
```

With the default configuration, `docker/config/claude.json` (`{"mcpServers": {}}`), the gateway
registers no services, but it answers, which confirms the stack works before you wire in real
services.

If you ran an earlier version of this stack, stop it first. It ran as the compose project
`docker` and published port 7001 on every interface:
`docker compose -p docker -f docker/docker-compose.yml down`. It also read the configuration
file named by `WEBSPEC_CONFIG_HOST`. This stack reads a directory instead, named by
`WEBSPEC_CONFIG_DIR`, and its gateway does not start while `WEBSPEC_CONFIG_HOST` is set, in
your shell or in `docker/.env`; see [Service configuration](#service-configuration).

To try the guide's demo server, which ships in the image, make a directory for its
configuration, `mkdir demo`, and save this in it as `demo/claude.json`:

```json
{
  "mcpServers": {
    "notes": {
      "command": "python",
      "args": ["/app/gateway/examples/demo_server.py"],
      "env": {"FASTMCP_CHECK_FOR_UPDATES": "off", "PYTHONNOUSERSITE": "1"}
    }
  }
}
```

The `env` stops the server from asking PyPI for updates at startup, and from running Python code
that other stdio servers could plant in HOME; see
[Bring your own MCP service](#bring-your-own-mcp-service).

Then point the stack at the directory and call a tool:

```bash
WEBSPEC_CONFIG_DIR=$PWD/demo docker compose -f docker/docker-compose.yml up -d
curl -s "http://notes.localhost:7001/read_note?id=welcome"
# {"result":"Hello from WebSpec."}
```

`curl` resolves `*.localhost` to a loopback address by itself. With another client, connect to
`127.0.0.1:7001` and send the `Host` header yourself.

## Guard key

The gateway needs a guard key (`WEBSPEC_GUARD_KEY`) for every guarded request, and for every
unsafe request at any level. Without one it still boots, and fails closed on those requests.

- **Set it directly.** Export `WEBSPEC_GUARD_KEY` before `docker compose up`. It is passed to
  the `gateway` container as is.
- **Read it from 1Password.** Set `OP_GUARD_KEY_REF` to an `op://` secret reference.
  `docker/entrypoint.sh` runs `op read "$OP_GUARD_KEY_REF"` at startup when `WEBSPEC_GUARD_KEY`
  is empty. That needs the `op` CLI and a service-account token inside the container, so build
  a derived image for it. If `OP_GUARD_KEY_REF` is set but `op` is missing, the entrypoint exits
  instead of booting an unguarded gateway. By default `op` reads its settings from HOME, which
  is the state volume and writable by the stdio MCP servers, so set `OP_CONFIG_DIR=/tmp/op` in
  that image: `/tmp` is a tmpfs that starts empty with the container, before any stdio server
  runs.

Either way the key reaches the gateway in its environment. Set directly, it is also in the
container's environment, which the init (PID 1) holds. The gateway and the init are both
non-dumpable (DP-2), so the stdio MCP servers the gateway runs cannot read the key in either
process, even though they run as the same uid 10001. That is why this stack does not use a key
file (`WEBSPEC_GUARD_KEY_FILE`): any file the gateway can read, a stdio server can read too. A
key file would not keep the key from the Docker daemon either, because `docker cp` copies it
out.

Two things do expose the key:

- **The Docker daemon.** Anyone who can reach it can read the key: `docker inspect` shows the
  container's environment. See [Trust boundary](#trust-boundary).
- **`docker compose exec gateway`.** The process it starts gets the container's environment,
  the guard key and any other secret in it included, and DP-2 does not cover it: a stdio MCP
  server can read that environment for as long as the process runs. Do not exec into a gateway
  that runs stdio servers. Run a separate container instead, as the
  [audit log commands](#the-audit-log-and-the-state-volume) do.

## Service configuration

The gateway reads its MCP servers from `/config/claude.json`, a file shaped like
`~/.claude.json`. Compose mounts a directory read-only at `/config`, `docker/config/` by
default. Point `WEBSPEC_CONFIG_DIR` at another directory to use it instead:

```bash
WEBSPEC_CONFIG_DIR=/etc/webspec-docker docker compose -f docker/docker-compose.yml up -d
```

The file in the directory must be called `claude.json`. Mount a directory that holds nothing
else: the gateway's container can read everything in it.

The gateway checks the file every 30 seconds and reloads it when it changes.
So whoever can write the file, or the directory that holds it, controls the gateway (DP-4). Keep
both where the agent cannot write them, such as a root-owned `/etc/webspec-docker`.
`docker/config/` is fine for a first run, but an agent that can edit your checkout can edit it
too. For the same reason, deploy from a checkout the agent cannot write: Caddy reads
`docker/caddy/Caddyfile` from it at startup.

The stack mounts the directory, not the file, so that an edit that replaces the file reaches the
gateway too. Many tools save a file by writing a new one and renaming it over the old one
(`webspec-ctl`, `sed -i`, `mv`, most editors), and a container that mounts a single file does
not see the new one until it is restarted: Docker Engine keeps showing the old file, and Docker
Desktop cannot open it. The gateway compares the file's identity, size, modification time and
change time, so a replacement that keeps the old modification time (`cp -p`, `install -p`,
`rsync -a`, `touch -r`) applies too.

On Linux, a bind mount keeps the host's owner and mode, so uid 10001 must be able to search the
directory and read the file. Make both root-owned, the directory with mode `0755` and the file
with `0644` (it holds no secrets; reference them as `${VAR}`), or make them `root:10001` with
modes `0750` and `0640`. The same goes for `docker/caddy/Caddyfile` and Caddy, which runs as
uid 10002: keep the file at mode `0644`, or make it `root:10002` with mode `0640`. If Caddy
cannot read it, its container exits at startup with `permission denied`.

`${VAR}` references in the file are filled from the gateway container's environment. Give the
gateway those variables in an override file, for example with an `env_file:` that only root can
read.

When the gateway cannot read `claude.json`, it starts with no services, and the entrypoint logs
why:

- `cannot find /config/claude.json`: the file is not in the directory. The gateway picks it up
  within 30 seconds of its appearing.
- `cannot search /config`: uid 10001 cannot search the directory. The gateway picks the file up
  within 30 seconds of the directory's permissions allowing it.
- `cannot read /config/claude.json`: the file is there, but uid 10001 cannot read it. Fix its
  permissions. The gateway picks the file up within 30 seconds, because `chmod` and `chown`
  change the file's change time.

If the file holds malformed JSON, the gateway logs a warning and keeps the services it last
loaded, if any.

`WEBSPEC_CONFIG_DIR` must name the directory, not the file. Named after the file, Docker mounts
that file at `/config`, where the gateway could never find `claude.json`. The gateway then does
not start, and its container restarts again and again with
`entrypoint: /config is not a directory` in its log.

Earlier versions of this stack read the file named by `WEBSPEC_CONFIG_HOST`. While that
variable is set, the gateway does not start, and its container restarts again and again with
`entrypoint: WEBSPEC_CONFIG_HOST (...) is retired and not read` in its log
(`docker compose -f docker/docker-compose.yml logs gateway`). Otherwise an upgraded stack would
silently serve `docker/config/` from the checkout in place of the protected file that variable
names. Move the file into a directory of its own as `claude.json`, set `WEBSPEC_CONFIG_DIR` to
that directory, and unset `WEBSPEC_CONFIG_HOST`, in `docker/.env` too.

## The audit log and the state volume

The gateway runs with `HOME=/var/lib/webspec` and writes its audit log to
`/var/lib/webspec/gateway-audit.jsonl`. That directory is the named volume `webspec-state`
(`webspec_webspec-state` on the host, since the compose project is called `webspec`), owned by
uid 10001 with mode `0700`. It is the only place, besides a small `/tmp`, where the gateway can
write.

Work on the log from a throwaway container of the gateway image, not with
`docker compose exec`, which would hand the guard key to a process the stdio servers can read
(see [Guard key](#guard-key)). This container carries no secrets, has no network, and sees the
volume read-only at `/audit`. Compose names the image `webspec-gateway`, after the project
`webspec`; if you pass `-p`, adjust the image and volume names to match.

```bash
audit() {
  docker run --rm --network none --read-only --cap-drop ALL --init=false \
    --security-opt no-new-privileges --user 10001:10001 \
    -v webspec_webspec-state:/audit:ro webspec-gateway "$@"
}
audit_stdin() {
  docker run --rm -i --network none --read-only --cap-drop ALL --init=false \
    --security-opt no-new-privileges --user 10001:10001 \
    -v webspec_webspec-state:/audit:ro webspec-gateway "$@"
}
```

`audit_stdin` hands its standard input to the container, which only the segment check below
needs. `audit` does not, on purpose: `docker run -i` reads all of its standard input, so in a
script fed to the shell that way (`bash -s < steps.sh`, `ssh host 'bash -s' < steps.sh`), it
would swallow the rest of the script, and the script would end after its first `audit` call,
without an error. `--init=false` matters only where the Docker daemon gives every container an
init (`"init": true` in `daemon.json`): the image's init refuses to start under another one
(see [Child processes](#bring-your-own-mcp-service)).

Read the log, check its hash chain (`None` means intact), and copy it out, for example to ship
it off the host:

```bash
audit cat /audit/gateway-audit.jsonl
audit python -I -c 'from pathlib import Path; from webspec.audit import verify_chain; print(verify_chain(Path("/audit/gateway-audit.jsonl")))'
audit cat /audit/gateway-audit.jsonl > gateway-audit.jsonl
```

Rotate the log by renaming the file while the gateway runs
([AU-4](../docs/spec/audit-deployment.md#audit)). That needs the volume writable. Each segment
gets a name of its own, and `mv -n` never replaces an existing file, so rotating again does not
overwrite an earlier segment:

```bash
docker run --rm --network none --read-only --cap-drop ALL --init=false \
  --security-opt no-new-privileges --user 10001:10001 \
  -v webspec_webspec-state:/audit webspec-gateway \
  mv -n /audit/gateway-audit.jsonl "/audit/gateway-audit.$(date -u +%Y%m%dT%H%M%SZ).jsonl"
```

The chain runs on across segments: the first `prev` of a segment is the hash of the previous
segment's last line. Check every segment, oldest first:

```bash
audit_stdin python -I - <<'EOF'
import hashlib
from pathlib import Path
from webspec.audit import GENESIS, verify_chain
prev = GENESIS
for log in sorted(Path("/audit").glob("gateway-audit.*.jsonl")) + [Path("/audit/gateway-audit.jsonl")]:
    if log.exists():
        print(log.name, verify_chain(log, first_prev=prev))
        lines = log.read_bytes().splitlines()
        prev = hashlib.sha256(lines[-1]).hexdigest() if lines else prev
EOF
```

If the gateway had written nothing since it last started when you renamed the file, or
restarted before its next write, the next segment starts again from 64 zeros and shows a
break at line 1 here; check that segment alone with
`verify_chain(path)`. If you deleted older segments, start `prev` from the last hash you kept
([AU-3](../docs/spec/audit-deployment.md#audit)).

`docker compose down` keeps the volume. `docker compose down -v` deletes it, and the audit log
with it.

## Restarts

Both services restart unless you stop them (`restart: unless-stopped`): after a crash, after
any exit of the gateway, and after the Docker daemon restarts or the host reboots (with Docker
Desktop, once it runs again). `docker compose stop` and `docker compose down` stop them for
good. A gateway stopped that way shows exit code 143 (128 + SIGTERM) after a clean shutdown:
uvicorn ends on the signal it was sent. Docker gives both services 40 seconds to stop
(`stop_grace_period`) before it kills them. The gateway waits up to 35 seconds for the tool
calls in progress, and Caddy, which `docker compose stop` stops first, for the answers it is
passing on; Docker's default wait, 10 seconds or less, would cut them short.

- A stdio MCP server runs as the gateway's user, so it can still signal the gateway. A signal
  that ends the gateway (`SIGTERM`, `SIGKILL`) ends the container, which comes back within
  seconds, with the gateway's in-memory state (nonces, pins, clearances, approvals, idempotency
  records; DP-7) empty. A signal that only stops it, such as `SIGSTOP`, would leave the
  container running and serving nothing, with no restart, so the init continues the gateway at
  once, its state intact, and logs `webspec-init: child <pid> stopped by SIGSTOP; continuing it`.
- The init binds the gateway's port, 7002, before it starts anything, and holds it for the
  container's life (`--listen` in the image's `ENTRYPOINT`); the gateway serves on the socket it
  is handed. When the gateway exits, the init kills every other process in the container, the
  stdio servers and whatever they left behind, and lets the port go only when none is left, or
  2 seconds later if one it cannot kill is still running: a process of another user, such as one
  that `docker exec -u 0` started, which no stdio server can start. It then logs
  `webspec-init: other processes are still running 2 seconds after ... exited; letting its port
  go anyway`. So neither while the gateway shuts down, which on `SIGTERM` can take up to 35
  seconds, nor after, can a stdio server listen on that port and receive what
  Caddy forwards there. Connections that arrive meanwhile wait, and are reset when the container
  exits. The init also holds `[::]:7002`, without listening there, so that nothing can listen on
  the port over IPv6 either (see [Ports](#ports)). The gateway logs `socket held by webspec-init`
  when it starts.
- `WEBSPEC_HOST`, `0.0.0.0` in the stack, must be an IP address. The gateway takes the socket
  only when it is bound to a loopback address or to exactly the address `WEBSPEC_HOST` names
  (DP-8), so the init refuses a host name, such as the service name `gateway`.
- A container that fails at startup is started again and again, with a delay that doubles up to
  a minute, instead of staying down. That includes the entrypoint's refusals to start when
  `OP_GUARD_KEY_REF` is set but `op` is missing, while `WEBSPEC_CONFIG_HOST` is set, and when
  `WEBSPEC_CONFIG_DIR` names a file, and the init's when `WEBSPEC_HOST` is not an IP address
  (`webspec-init: refusing to start ...: WEBSPEC_HOST 'gateway' is not an IP address`). After a
  change, check that both containers stay up: `docker compose -f docker/docker-compose.yml ps`.

## Ports

Caddy is published on `127.0.0.1:7001` and `[::1]:7001`, both loopback. Holding both keeps any
other local user from listening on `[::1]:7001` and receiving the requests, guard tags, nonces
and `GET` arguments included, of clients that try `::1` first for `*.localhost`, as Python,
Node and Go do. Allow the agent these two addresses at port 7001 (DP-3).

Docker holds these addresses only while the `caddy` container runs. Whenever that container is
down, for as long as Docker takes to start it again (a fraction of a second after one crash or a
`docker restart`, and longer as crashes repeat, since Docker doubles its delay each time),
Docker lets them go, and any local process can listen there and receive what is sent to them
meanwhile. A process that still holds a port when Docker starts Caddy again keeps it. On a Linux
host, Docker Engine then fails the restart and does not try again; Docker Desktop starts Caddy
without that port and publishes it again once the port is free. Either way that process receives
every local request on the port until it stops: stop it, then start Caddy with `docker compose
-f docker/docker-compose.yml up -d caddy`. So DP-9 holds in this stack for the gateway's port,
which the init holds, and not for Caddy's. Keep users you do not trust off the host, and run
cloudflared as a service of this stack (see [Exposing the stack
publicly](#exposing-the-stack-publicly)): it reaches Caddy over the stack's network, never
through a host port.

Two kinds of host differ:

- **No IPv6 on the loopback interface.** Docker cannot bind `[::1]:7001`, and the `caddy`
  container does not start (`failed to bind host port [::1]:7001/tcp: cannot assign requested
  address`). Add the override that publishes `127.0.0.1:7001` alone, which needs Docker Compose
  2.24.4 or later: `docker compose -f docker/docker-compose.yml -f docker/ipv4-only.yml up -d`.
  Clients cannot reach `::1` on such a host and fall back to `127.0.0.1`, so nothing is lost.
- **Linux with the userland proxy turned off** (`"userland-proxy": false` in
  `/etc/docker/daemon.json`). Docker cannot forward `[::1]` to this stack's IPv4-only network,
  so it skips that mapping. The daemon logs `Cannot map from IPv6 to an IPv4-only container
  because the userland proxy is disabled`, `docker port webspec-caddy-1` lists `127.0.0.1:7001`
  only, and `[::1]:7001` stays free for anyone. Turn the userland proxy back on, or connect every
  client to `127.0.0.1:7001` and allow the agent that address only. Without the userland proxy,
  Docker also rewrites connections to `127.0.0.1:7001` to the container's address (DNAT in the
  `nat` table's `OUTPUT` chain). A firewall rule that confines the agent has to match before
  that, for example an nftables output chain at priority `mangle`; at the default `filter`
  priority it sees the container's address and refuses the agent.

Inside the stack, Caddy forwards to `gateway:7002` over the stack's own network, which
`docker-compose.yml` keeps IPv4-only (`enable_ipv6: false`), whatever the Docker daemon's
default for new networks. On a network with a global IPv6 prefix, Docker's DNS would also give
Caddy the gateway's IPv6 address, which Caddy dials first, and where the gateway does not
listen: a stdio server listening on `[::]:7002` would receive what Caddy forwards. The init
holds that address without listening (see [Restarts](#restarts)), so such connections are
refused and Caddy falls back to IPv4. Keep the network IPv4-only all the same, and both services
on it alone. `docker network inspect webspec_default` shows `"EnableIPv6": false`; if it does
not, as for a network an earlier version of the stack created with IPv6,
`docker compose -f docker/docker-compose.yml down` and `up -d` recreate it.

## Exposing the stack publicly

Caddy is published on the loopback addresses only, so nothing off the host reaches the stack
directly. To serve a public domain, run a Cloudflare tunnel in front of Caddy and set
`WEBSPEC_DOMAIN`:

```bash
WEBSPEC_DOMAIN=example.com docker compose -f docker/docker-compose.yml up -d
```

This stack's Caddy applies no rate limit: the stock image has no rate-limit module. Online
guessing of the gateway's 32-bit tags is then limited only by what reaches Caddy, so limit the
request rate at the edge before you expose it, for example with a Cloudflare rate-limiting
rule ([Known limits](../docs/spec/status.md#known-limits)).

With `WEBSPEC_DOMAIN` set, Caddy also forwards `*.example.com`, and the gateway serves
`{destination}.example.com`. A destination on the public domain must be at level 1 or higher;
at level 0 it answers `403 unguarded_public` there
([HG-7](../docs/spec/addressing.md#host)). Use a real guard key.

**cloudflared on the host.** Point the tunnel's ingress at Caddy's loopback port, by address:

```yaml
# /etc/cloudflared/config.yml
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json
ingress:
  - hostname: "*.example.com"
    service: http://127.0.0.1:7001
  - service: http_status:404
```

**cloudflared as a service of this stack.** Add it in an override file of your own, and in the
Cloudflare dashboard route the tunnel's public hostname `*.example.com` to
`http://caddy:7001`:

```yaml
services:
  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run
    environment:
      TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN:?set CLOUDFLARE_TUNNEL_TOKEN}
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    restart: unless-stopped
    depends_on: [caddy]
```

Prefer the second. Docker lets Caddy's host port go whenever the `caddy` container is down, and
a tunnel on the host would then send whatever reaches it to any process that listens there
(see [Ports](#ports)); over the stack's network it reaches Caddy or nothing.

Either way, send the tunnel to Caddy, never to `gateway:7002`, and do not set an HTTP host header
override (`httpHostHeader`) on the tunnel. Caddy and the gateway route on the `Host` header the
client sent, and the guard signs it. A request that arrives through the tunnel with a loopback
`Host` anyway gets `421` from Caddy: Cloudflare marks every request it forwards with a `Cf-Ray`
header, and Caddy never forwards a loopback host that carries one.

## Trust boundary

- **The Docker daemon is root.** Whoever can talk to it can read the guard key and rewrite the
  audit log. Keep the agent away from it. On Linux, do not put the agent's user in the `docker`
  group. Docker Desktop on macOS gives its socket to the logged-in user, so if the agent runs as
  that user, this stack does not meet DP-1. Run the agent as another user, or use the native
  deployment under `gateway/deploy/`.
- **Stdio MCP servers share the gateway's user and HOME.** They cannot read the environment or
  memory of the gateway or the init (DP-2), and the gateway's Python ignores user site-packages,
  so code they plant in HOME does not run inside the gateway. But they can read any file the
  gateway can, and they can write `/var/lib/webspec`, audit log included. Code one stdio server
  plants in HOME can also run in the others. The gateway starts every stdio server with
  `PYTHONNOUSERSITE=1`, so Python ignores a `.pth` file under
  `~/.local/lib/python3.11/site-packages`, unless a server's entry turns that off. But other
  runtimes read settings from HOME (`~/.npmrc`, `~/.gitconfig`). Such files outlive restarts and
  rebuilds, because HOME is the state volume; only `docker compose down -v` removes them, and the
  audit log with them. Any stdio server can also end the gateway, which restarts the container with
  the gateway's in-memory state empty (see [Restarts](#restarts)). Run only
  stdio servers you trust as much as the gateway itself, prefer HTTP MCP servers in containers of
  their own, and ship the log off the host.
- **Loopback publishing.** Older Docker Engine releases on Linux could let hosts on the same
  network segment reach a port published on 127.0.0.1. Keep Docker Engine current, or also block
  port 7001 in the host firewall.

## Bring your own MCP service

- **HTTP services** (MCP over HTTP) can join the stack as services of their own. Add them in an
  override file and reference them from the configuration as `http://<service>:<port>`.
- **Stdio services** run inside the gateway container, as uid 10001 with a read-only root
  filesystem: the gateway starts the configured `command` there, not on the host. The image has
  Python and the gateway's dependencies, including `fastmcp`. Anything else needs a derived image
  (`FROM` the gateway image, keeping `USER 10001:10001`). A stdio server does not get the
  gateway's environment: it gets `HOME`, `PATH` and `PYTHONNOUSERSITE=1`, plus the `env` its
  entry sets, starts in `/` (give its files by absolute path), and can write only `/tmp` and
  `/var/lib/webspec`. `PYTHONNOUSERSITE=1` makes a Python server ignore Python code other
  servers could plant in HOME (see [Trust boundary](#trust-boundary)). The gateway sets it
  because it ignores user site-packages itself, as the image's `python -I` and
  `PYTHONNOUSERSITE=1` make it; a gateway that does not, elsewhere, does not set it. So set `"PYTHONNOUSERSITE": "1"` in every Python server's `env` too, as the demo
  entry does, and the entry stays safe wherever you use it. `"PYTHONNOUSERSITE": ""` turns user
  site-packages back on: don't. HOME is the state volume, so whatever a server caches under HOME
  lands next to the audit log. FastMCP
  servers, for example, keep a version cache there and ask PyPI for updates at startup, unless
  their entry sets `"env": {"FASTMCP_CHECK_FOR_UPDATES": "off"}`.
- **Child processes.** PID 1 is the image's init (`docker/init.py`), not the gateway. It waits
  for any process a stdio server leaves behind, so none of them stays a zombie, and it continues
  the gateway when a stdio server stops it (see [Restarts](#restarts)). The stack never uses
  Docker's own init: docker-init would be PID 1 as uid 10001 and dumpable, and every stdio server
  could read the container's environment, guard key and service secrets included, from
  `/proc/1/environ`. Compose sets `init: false`, which also overrides a Docker daemon that gives
  every container an init (`"init": true` in `daemon.json`). Should the image run under another
  PID 1 whose environment its user can read anyway, as with `docker run --init`, its init refuses
  to start: `refusing to start ...: PID 1 (/sbin/docker-init) is another init`.

Level 4 (human approval) needs `ssh-keygen` and an allowed-signers file (`WEBSPEC_APPROVERS_FILE`).
The image ships neither, so a request that needs approval gets `503 approval_unavailable` until
you add them, with a derived image and a read-only mount.

## The registry service (experimental)

The `webspec_registry` package (semantic tool resolver and keychain poset graph) is
experimental and not part of the hardened deployment. Its block in `docker-compose.yml` stays
**commented out on purpose**:

- It has no authentication and no Caddy route. It binds **localhost only**
  (`WEBSPEC_REGISTRY_HOST`, default `127.0.0.1`, at `WEBSPEC_REGISTRY_PORT`, default `7004`) and
  shows your tool inventory to every local process. Exposing it safely (a ported guard
  middleware or a Caddy front) is tracked as a `TODO(C)` in `docs/spec/status.md`.
- It has no dedicated user and none of the gateway's process hardening. **Never give it the
  guard key** (`WEBSPEC_GUARD_KEY` or `WEBSPEC_GUARD_KEY_FILE`), and leave
  `WEBSPEC_REGISTRY_HARVEST_GUARDED` unset, so that it never reads the key: DP-1 keeps the key in
  the gateway's environment, and a registry running as you, or as the agent, would hand it to
  every process of that user. Without the key it lists unguarded services only, and skips the
  others with a warning.

To try it against this stack, run it on the host, from a shell that holds no guard key, and
point it at Caddy's address:

```bash
env -u WEBSPEC_GUARD_KEY -u WEBSPEC_GUARD_KEY_FILE \
  WEBSPEC_GATEWAY_URL=http://127.0.0.1:7001 python -m webspec_registry   # or: webspec-registry
# then, from the same host only:
curl http://127.0.0.1:7004/catalog
curl "http://127.0.0.1:7004/resolve?q=send+a+message"
curl "http://127.0.0.1:7004/graph?format=dot"
```

The registry connects to the address in `WEBSPEC_GATEWAY_URL` and sets the `Host` header
itself: `localhost` for the gateway's index and `{service}.localhost` for each service, which
are what Caddy forwards. It refuses, and exits, when the URL names a host that resolves to a
loopback address, such as `localhost`: that name resolves to `::1` first, where another local
user could be listening ([Ports](#ports)).
