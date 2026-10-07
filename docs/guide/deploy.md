# Deploying

A gateway on your laptop is fine for trying WebSpec. A gateway that an agent works through
every day needs a deployment that meets the deployment rules,
[DP-1 to DP-9](../spec/audit-deployment.md#deployment), and keeps the guard key out of reach
([GD-5](../spec/levels.md#level-1-signed)). The repository ships one for each platform:

| Platform | Files | What runs it |
|---|---|---|
| Linux | `gateway/deploy/linux/`, `gateway/tools/setup-caddy.sh` | systemd units for the gateway and for Caddy |
| macOS | `gateway/deploy/macos/` | a LaunchDaemon (no proxy is shipped) |
| Docker | `docker/` | a compose stack: the gateway and Caddy |

`gateway/systemd` and `gateway/launchd` are development units. They run the gateway as your
login user, so they meet neither DP-1 nor DP-4. Don't expose them.

## The shape of a deployment

```text
Internet ─► Cloudflare Tunnel (cloudflared)
              │ *.example.com              → 127.0.0.1:7001
              │ direct sites, e.g. web.…   → 127.0.0.1:7003
              ▼
            Caddy (user caddy). systemd holds 127.0.0.1 and [::1], ports 7001 and 7003
              │ forwards only hosts that have a site block; 421 for anything else
              ▼ dials 127.0.0.1:7002 by IP, Host header unchanged
            WebSpec gateway (user webspec). systemd holds 127.0.0.1:7002
              ├─ stdio MCP servers: its children, same user
              └─ HTTP MCP servers: by URL

The agent runs as its own user and may reach only 127.0.0.1:7001 and [::1]:7001 (DP-3).
```

Three ideas hold it together:

- **Nobody can stand in for a listener.** On Linux, systemd binds every port and keeps it bound
  while Caddy or the gateway restarts; launchd does the same for the gateway's port on macOS,
  and the container's init for the gateway's port in Docker. If the processes bound their
  own ports, any local process could take a port in the gap between one process and the next,
  and receive full URLs, bodies, and the signed headers ([DP-9](../spec/audit-deployment.md#deployment)).
- **The agent reaches the gateway only through the proxy.** Caddy listens on loopback only. It
  serves a service's public name only when the service is at level 1 or higher, and it never
  serves a `*.localhost` name that came through the tunnel ([DP-5](../spec/audit-deployment.md#deployment)).
  Sites that bypass the gateway are served on a port of their own, `7003`, which the agent's
  egress allow-list leaves out ([DP-3](../spec/audit-deployment.md#deployment)).
- **What configures the gateway is root's.** The code, the configuration, the secrets, and the
  units can be changed only by root ([DP-4](../spec/audit-deployment.md#deployment)), and the
  installers refuse to run code that another user could have changed.

## Who can do what

The table is for Linux. macOS is the same with `_webspec` in place of `webspec`, except that
there the stdio servers can also read the guard key, the secrets file, and the gateway's
environment: macOS has no non-dumpable process.

| Account | Runs | Can read | Can write |
|---|---|---|---|
| The agent's user | the agent | the unit files and `/etc/caddy`, which hold no secrets | its own files. It can reach Caddy on `:7001` and nothing else, once you set up DP-3 |
| `webspec` | the gateway and every stdio MCP server | `/etc/webspec/config.json` and `allowed_signers`, but not `gateway.env` | `/var/lib/webspec`: its home, and the audit log |
| `caddy` | Caddy | `/etc/caddy` | `/var/log/caddy` |
| root (you, through `sudo`) | the installers and `webspec-ctl` | everything | `/opt/webspec`, `/etc/webspec`, `/etc/caddy`, the units |

The agent's user must not be `webspec` or root. It must not be in the groups `webspec`,
`sudo`, `docker`, `adm`, or `systemd-journal`: the journal holds the gateway's log lines and
whatever the stdio servers print. On Debian, keep it out of `staff` too, which can write
`/usr/local`. On macOS, keep it out of `_webspec` and `admin`, the group that may use `sudo`.

Every stdio MCP server runs as the gateway's user. On Linux the gateway makes itself
non-dumpable, so the servers can't read its memory or environment. They can still read each other's
environment, write the audit log, and end the gateway, which then restarts with its in-memory
state empty ([DP-7](../spec/audit-deployment.md#deployment)). Run only stdio servers you trust as
much as the gateway, and run the others as HTTP servers under users of their own.

## Linux

You need systemd 247 or newer, Linux 5.8 or newer, git, curl, and Python 3.11 or newer with
`venv` (`apt install git curl python3-venv` on Debian and Ubuntu). `setup-caddy.sh` builds
Caddy with Go 1.21 or newer, and downloads a pinned Go with curl when there is none. Run every
command here from an account that the agent does not use.

### Install

1. **Clone the repository where only root can change it**, and check out a commit you have
   reviewed. The installers refuse a checkout that any other user can change, and they cannot
   vouch for files that were changed before root got them:

    ```bash
    sudo git clone https://github.com/I-m-A-g-I-n-E/WebSpec.git /opt/webspec/src
    sudo git -C /opt/webspec/src checkout --detach <reviewed-commit>
    ```

2. **Read the plan, if you like.** A dry run needs no root and changes nothing:

    ```bash
    /opt/webspec/src/gateway/deploy/linux/install.sh --dry-run
    ```

3. **Install the gateway**, with plain `sudo`, never `sudo -E`:

    ```bash
    sudo /opt/webspec/src/gateway/deploy/linux/install.sh
    ```

    It creates the `webspec` account, the virtual environment in `/opt/webspec/venv`, the files
    in `/etc/webspec`, and the two units. It starts `webspec-gateway.socket`, so systemd holds
    `127.0.0.1:7002` from now on. It does not start the gateway, because there is no key yet. Add
    `--python=/usr/bin/python3.12` if the `python3` on the system path is older than 3.11. The
    installer refuses an interpreter, a standard library, or a pip configuration that a user
    other than root can change, including where a link in the standard library leads (Debian's
    `sitecustomize.py` leads to `/etc/python3.X/`). It also refuses an existing `webspec` account
    that someone can log in to. `--help` lists the options that override the interpreter and
    account refusals. A pip configuration that others can change has no override: fix its owner.
    It refuses systemd drop-ins for its two units in `/etc` or `/run`, unless you pass
    `--allow-drop-ins` for ones you have reviewed. Even then it refuses a drop-in that changes
    who runs what, the environment, the sandbox, or the listener: variables for the gateway
    belong in `gateway.env`.

4. **Add the guard key.** Generate 64 hex digits once (`openssl rand -hex 32`), keep them in
   your password manager, and pipe them in as root, so they never appear on a command line:

    ```bash
    op read 'op://WebSpec/gateway-guard/key' \
      | sudo sh -c 'IFS= read -r k || [ -n "$k" ] && printf "WEBSPEC_GUARD_KEY=%s\n" "$k" >> /etc/webspec/gateway.env'
    ```

5. **List your MCP servers** in `/etc/webspec/config.json`, in the format of
   `/etc/webspec/config.example.json`. Each secret is written there as `"${NAME}"`, and its
   value goes in `/etc/webspec/gateway.env` as `NAME=value`. Give every stdio server an absolute
   command that root owns, outside every home directory, for example under
   `/opt/webspec/services/`. Edit both files as root with a fixed editor:

    ```bash
    sudo -H /usr/bin/vi /etc/webspec/config.json
    sudo -H /usr/bin/vi /etc/webspec/gateway.env
    ```

    For a public domain, add it the same way: `WEBSPEC_DOMAIN=example.com` in `gateway.env`.

6. **Add the people who may approve** level-4 requests, one OpenSSH allowed-signers line each
   ([Human approval](approval.md)):

    ```bash
    sudo -H /usr/bin/vi /etc/webspec/allowed_signers
    ```

7. **Start the gateway.** Run the installer again. With a usable key in place, it enables and
   starts the gateway, then checks that it runs as `webspec`, that systemd holds
   `127.0.0.1:7002`, and that the gateway answers. A key of only whitespace or invisible
   characters counts as none: the gateway refuses to start, and the installer says so.

    ```bash
    sudo /opt/webspec/src/gateway/deploy/linux/install.sh
    ```

8. **Put Caddy in front** ([DP-5, DP-6](../spec/audit-deployment.md#deployment)). The script
   builds Caddy 2.11.7 with the rate-limit plugin, which needs Go (it downloads a pinned,
   checksummed Go if you have none):

    ```bash
    sudo WEBSPEC_DOMAIN=example.com /opt/webspec/src/gateway/tools/setup-caddy.sh
    ```

    Or it takes a Caddy you built with the rate-limit plugin from `WEBSPEC_CADDY_BINARY`. The
    binary, and every directory above it, must be root's alone, and the variable must be on
    `sudo`'s command line, on every run, or the script builds Caddy itself:

    ```bash
    sudo /usr/bin/install -o root -g root -m 0755 ./caddy /root/caddy
    sudo WEBSPEC_DOMAIN=example.com WEBSPEC_CADDY_BINARY=/root/caddy /opt/webspec/src/gateway/tools/setup-caddy.sh
    ```

    It runs Caddy as the `caddy` user, with every listener held by `caddy-webspec.socket`, and
    writes a site block for every service in `/etc/webspec/config.json`. At the end it prints
    the cloudflared ingress rules. Use them as printed, and keep the original `Host` header (no
    `httpHostHeader`), because the guard signs it. Run cloudflared as the systemd unit
    `cloudflared.service` (`cloudflared service install`), with these rules in its
    configuration: the scripts stop and start that unit while ports change hands. Run the script again whenever you turn IPv6
    on or off at boot (`ipv6.disable=1`): the listeners it records depend on it.

    ```yaml
    ingress:
      - hostname: web.example.com        # each direct site first
        service: http://127.0.0.1:7003
      - hostname: "*.example.com"
        service: http://127.0.0.1:7001
      - service: http_status:404
    ```

9. **Confine the agent's egress** ([below](#egress-dp-3)). Nothing that ships does this for you.

### Files

| Path | Owner and mode | Holds |
|---|---|---|
| `/opt/webspec/src` | root, not writable by others | the reviewed checkout |
| `/opt/webspec/venv` | root, not writable by others | the gateway, and `bin/webspec-ctl` |
| `/etc/webspec/` | `root:webspec 0750` | the gateway's configuration |
| `/etc/webspec/config.json` | `root:webspec 0640` | the MCP servers, with secrets only as `${NAME}` |
| `/etc/webspec/gateway.env` | `root:root 0600` | `WEBSPEC_GUARD_KEY`, `WEBSPEC_DOMAIN`, and the MCP secrets. systemd reads it as root |
| `/etc/webspec/allowed_signers` | `root:root 0644` | the level-4 approvers. Whoever can add a line can approve |
| `/var/lib/webspec/` | `webspec 0700` | the home of the gateway and of every stdio server |
| `/var/lib/webspec/gateway-audit.jsonl` | `webspec 0600` | the hash-chained audit log |
| `/etc/systemd/system/webspec-gateway.{socket,service}` | `root 0644` | the gateway's units, replaced on every run |
| `/etc/caddy/Caddyfile`, `/etc/caddy/conf.d/` | `root`; files `0644`, directories `0755` | Caddy's configuration, generated |
| `/var/log/caddy/` | `caddy 0750` | Caddy's access logs, without query strings or headers |

The installers never overwrite `config.json`, `gateway.env`, or `allowed_signers`. They
re-apply the owners and modes on every run. Don't edit the units or the generated Caddy files:
the next run replaces them. Site settings belong in `gateway.env` and `config.json`.

### Day to day

Run `webspec-ctl` as root by its full path. On a production host it edits
`/etc/webspec/config.json` and `gateway.env`, and every command first prints the file it uses.

- **Add a service.** This writes the entry and Caddy's site block in one transaction, so
  if Caddy rejects the change, nothing changes:

    ```bash
    sudo /opt/webspec/venv/bin/webspec-ctl add tickets --url https://mcp.tickets.example/mcp \
      --header 'Authorization:Bearer ${TICKETS_TOKEN}' --secret TICKETS_TOKEN --level 3
    ```

    A stdio server takes `--command /opt/webspec/services/<name>/<program>` and `--args`
    instead. `--secret` adds an empty `TICKETS_TOKEN=` line to `gateway.env`: fill it as in
    step 4, then restart the gateway, which reads `gateway.env` only when it starts. A new
    service is at level 1 unless you give `--level`. `--no-guard`, and the `--port` shorthand
    for a local app, make it an unguarded level-0 service, served on loopback names only.

- **Remove a service**: `sudo /opt/webspec/venv/bin/webspec-ctl rm tickets --clean-env TICKETS_TOKEN`.
  The gateway drops it within 30 seconds.

- **Check what runs**: `sudo /opt/webspec/venv/bin/webspec-ctl ls`. A guarded service shows
  `ok (guarded)` when the gateway answers its unsigned probe with the guard's `401`; the MCP
  server behind it is not checked. `ls` reads the file, not what the gateway applied. Direct
  sites are not in `config.json`, so neither `ls` nor `health` lists them: they are the site
  blocks in `/etc/caddy/conf.d` whose second line records `"kind":"direct"`. Remove one with
  `sudo /opt/webspec/venv/bin/webspec-ctl rm <name>`, then remove its ingress rule.

- **Edit the configuration by hand**, then bring Caddy in line:
  `sudo -H /usr/bin/vi /etc/webspec/config.json` and
  `sudo /opt/webspec/venv/bin/webspec-ctl caddy-sync`. The gateway reloads the file within 30
  seconds of any change. A file it cannot load, malformed or not a valid registry, changes
  nothing: the gateway logs one warning and keeps what it had, so check
  `journalctl -u webspec-gateway.service` after an edit. A changed command, URL, or headers of
  an existing server applies when the gateway next connects to it, or when it restarts.

- **Serve a site that bypasses the gateway**, such as a local web app, with
  `sudo /opt/webspec/venv/bin/webspec-ctl add web --direct --port 3000`. It is served on the
  direct listener, `:7003`, with no guard, no audit log, and no rate limit. Add the ingress rule
  it prints, above the wildcard rule. The port must be the app's own: `webspec-ctl` refuses
  Caddy's ports and the gateway's. It also refuses to give a gateway service the name of a
  direct site unless you pass `--force`, and then reminds you to remove that site's ingress
  rule.

- **Change the public domain.** Caddy records the domain in its Caddyfile, so a routine
  `caddy-sync` keeps it. To change it, run all three steps:

    ```bash
    sudo WEBSPEC_DOMAIN=new.example /opt/webspec/venv/bin/webspec-ctl caddy-sync
    sudo -H /usr/bin/vi /etc/webspec/gateway.env      # WEBSPEC_DOMAIN=new.example
    sudo systemctl restart webspec-gateway.service
    ```

    Then point cloudflared at the new name. `caddy-sync --no-public` stops serving public names
    at all.

- **Restart** with `sudo systemctl restart webspec-gateway.service`. The socket keeps the port
  bound, and new connections wait for the new gateway. The old one stops taking connections,
  then for a quarter of a second answers what arrives on those it has open, each time with
  `Connection: close`, so that clients such as Caddy open their next connection to the new one.
  A stop signals the gateway alone and lets it finish the calls in flight, for up to 40
  seconds, before it kills what is left. A restart is needed after any
  change to `gateway.env`, and it forgets the in-memory state (nonces, approvals, idempotency
  records, pins).

- **Stop** in this order, and start again in the reverse order:

    ```bash
    sudo systemctl stop cloudflared.service
    sudo systemctl stop caddy-webspec.socket caddy-webspec.service
    sudo systemctl stop webspec-gateway.socket webspec-gateway.service
    ```

    If your tunnel runs some other way, stop it by its own means first.

    Stopping only `webspec-gateway.service` is not a stop: the next connection to the socket
    starts it again. While a socket is stopped, its port is free for any local process, which
    is why the tunnel stops first. To keep everything off across reboots, use
    `disable --now` with the same units.

- **Logs**: `journalctl -u webspec-gateway.service` for the gateway, which also carries what the
  stdio servers print; `/var/log/caddy/<service>.log` and `journalctl -u caddy-webspec.service`
  for Caddy.

- **The audit log.** Check the chain with:

    ```bash
    sudo /opt/webspec/venv/bin/python -I -c 'from pathlib import Path; from webspec.audit import verify_chain; print(verify_chain(Path("/var/lib/webspec/gateway-audit.jsonl")))'
    ```

    It prints `None` when the chain is intact, or the number of the first line that breaks it.
    The log records refused requests too, and nothing limits its size, so rotate it on a
    schedule and watch its disk use. Rotate it by renaming the file while the gateway runs
    ([AU-4](../spec/audit-deployment.md#audit)), never by truncating it. The gateway goes on
    chaining from the last line it wrote, so check the file that follows a rotation against the
    renamed one's last line, or it reports line 1:

    ```bash
    sudo /opt/webspec/venv/bin/python -I -c 'import hashlib; from pathlib import Path; from webspec.audit import verify_chain; old = Path("/var/lib/webspec/gateway-audit.jsonl.1").read_bytes().splitlines()[-1]; print(verify_chain(Path("/var/lib/webspec/gateway-audit.jsonl"), first_prev=hashlib.sha256(old).hexdigest()))'
    ```

    A gateway that had written nothing since it last started when you renamed the file, or
    that restarted before its next entry, starts the new file from the beginning of a chain,
    and the first command checks it. The stdio servers can write the
    log, so ship it, or at least its latest hash, off the host.

- **Upgrade**: bring `/opt/webspec/src` to a newer commit you have reviewed, then run
  `install.sh` and then `setup-caddy.sh` again, in that order, with the same
  `WEBSPEC_CADDY_BINARY` as before if you use a prebuilt Caddy.

### Moving off the development setup

If the gateway runs today as your login user, from `gateway/systemd` or from a hand-written
system unit, reading `~/.claude.json`:

1. Treat its guard key as exposed: your login user could read it. Make a new one.
2. While the old gateway still runs, write the new configuration as root. Copy into
   `/etc/webspec/config.json` only the servers you mean to expose. Give each an absolute
   command that root owns, outside every home directory, and turn each secret into
   `"${NAME}"`, with its value in `gateway.env`. Then add the new key as in step 4 above. The
   production gateway never reads `~/.claude.json`, and its stdio servers start in `/`, so
   relative paths fail.

    ```bash
    sudo install -d -m 0700 /etc/webspec
    sudo -H /usr/bin/vi /etc/webspec/config.json
    sudo -H /usr/bin/vi /etc/webspec/gateway.env
    ```

3. Stop the tunnel, stop the old gateway, run the installer, and start the tunnel again. Stop
   a gateway from `gateway/systemd` as your login user, with
   `systemctl --user disable --now webspec-gateway.service`. Stop one from a hand-written
   system unit with `sudo systemctl disable --now webspec-gateway.service`. Then:

    ```bash
    sudo systemctl stop cloudflared.service
    # stop the old gateway here, as above
    sudo /opt/webspec/src/gateway/deploy/linux/install.sh
    sudo systemctl start cloudflared.service
    ```

    The installer takes over the files you wrote and applies their owners and modes. It
    refuses to replace a gateway unit that it did not install while that unit runs, saves a
    copy of that unit, and prints these steps.

4. Run `setup-caddy.sh` to replace the old Caddy. It refuses to stop serving any host that
   Caddy serves today, unless you set `ALLOW_SHRINK=1`. It moves sites that bypassed the
   gateway to `:7003`, so update cloudflared with the rules it prints. It also moves the old
   logs aside. The old Caddy logged full URLs and guard headers to the journal; the script
   tells you how to clear them.
5. Stop using the old `webspec-ctl`.

## macOS

The macOS deployment runs the gateway as a LaunchDaemon, as the hidden user `_webspec`, with
launchd holding `127.0.0.1:7002`. It ships no proxy and no egress rules: those are yours.

1. **Clone where only root can change it:**

    ```bash
    sudo /bin/mkdir -p /opt/webspec
    sudo -H /usr/bin/git clone https://github.com/I-m-A-g-I-n-E/WebSpec.git /opt/webspec/src
    sudo -H /usr/bin/git -C /opt/webspec/src checkout --detach <reviewed-commit>
    ```

2. **Use a Python that only root can change**, 3.11 or newer: the python.org installer
   followed by `sudo /bin/chmod -R go-w /Library/Frameworks/Python.framework`, or MacPorts.
   The installer refuses Homebrew's Python, which belongs to whoever installed Homebrew.
3. **Unload a development LaunchAgent**, if one runs, as its user:
   `launchctl bootout gui/$(id -u)/com.webspec.gateway && launchctl disable gui/$(id -u)/com.webspec.gateway`.
4. **Install.** The first run creates the account, `/etc/webspec`, the virtual environment, and
   the daemon. Without a key it leaves the daemon disabled, and says so:

    ```bash
    sudo WEBSPEC_DOMAIN=example.com /opt/webspec/src/gateway/deploy/macos/install.sh
    ```

5. **Fill the key.** The installer writes the key to `/etc/webspec/guard.key`
   (`root:_webspec 0440`) in one rename. It refuses anything that is not a usable key, and
   leaves the old key in place when it does. Use an `op` that only root can change, such as the
   one the 1Password package installs:

    ```bash
    /usr/local/bin/op read 'op://WebSpec/gateway-guard/key' \
      | sudo /opt/webspec/src/gateway/deploy/macos/install.sh --fill-key
    ```

6. **Run the installer again.** It loads the daemon, and checks that the gateway took the socket
   launchd holds and that it answers.
7. **Add servers and secrets** as on Linux. `gateway.env` here is read by `/bin/sh`, so write
   `NAME='value'`. Add approvers to `/etc/webspec/allowed_signers`.
8. **Run a proxy** on `127.0.0.1:7001` and `[::1]:7001` that meets DP-5 and DP-6. It forwards
   to `127.0.0.1:7002`, by IP, only hosts under your domain, and `*.localhost` names only for
   connections from the host itself: it answers `421` to a request that carries Cloudflare's
   `Cf-Ray` header and names one. It keeps the `Host` header, and keeps query strings, request
   and response headers, and userinfo out of its logs. Point the tunnel at it only once it runs.
   `webspec-ctl` manages no Caddy on macOS.
9. **Confine the agent's egress** (DP-3).

Day to day, re-run the installer after editing `gateway.env`: it checks the file, then
restarts the daemon. `sudo /bin/launchctl kickstart -k system/com.webspec.gateway.daemon` restarts
it without the checks, and launchd keeps the port bound either way. A restart can take up to
40 seconds while the gateway finishes the calls in flight. A new key from
`--fill-key` applies to the next request, with no restart. The log is
`/var/log/webspec/gateway.log`, and it is not rotated. To stop the gateway, stop the proxy
first, then run
`sudo /bin/launchctl disable system/com.webspec.gateway.daemon && sudo /bin/launchctl bootout system/com.webspec.gateway.daemon`.

On macOS, give `sudo` every program by its absolute path, as above. macOS keeps your `PATH`
under `sudo`, so a bare name runs whatever comes first on it, and the agent may be able to
write a directory there, such as Homebrew's.

On macOS there is no non-dumpable process (DP-2), and every stdio server can read
`guard.key`, `gateway.env`, and `config.json`. The `_webspec` user is the only boundary.

## Docker

The compose stack runs the gateway as uid 10001 behind Caddy, both with read-only root
filesystems, on a network pinned to IPv4. Caddy is published on `127.0.0.1:7001` and
`[::1]:7001` only. The image's own init runs as PID 1 and makes itself non-dumpable. It holds
the gateway's port for the container's whole life, and when the gateway exits it kills the
container's other processes before it lets the port go. It also reaps orphaned processes. A
stop gives the gateway 40 seconds to finish the calls in flight.

```bash
cd /path/to/WebSpec                              # a checkout the agent cannot write
sudo /usr/bin/install -d -m 0755 /etc/webspec-docker   # a directory that holds only claude.json
sudo -H /usr/bin/vi /etc/webspec-docker/claude.json
export WEBSPEC_GUARD_KEY=$(op read 'op://WebSpec/gateway-guard/key')   # an op that only root can change
WEBSPEC_CONFIG_DIR=/etc/webspec-docker docker compose -f docker/docker-compose.yml up --build -d
```

Docker holds Caddy's published port only while Caddy's container runs, and lets it go whenever
that container is down, so a local process could take it then
([DP-9](../spec/audit-deployment.md#deployment)). One that still holds it when Docker starts
Caddy again keeps it, and receives every local request on it until it stops
([docker/README.md](https://github.com/I-m-A-g-I-n-E/WebSpec/blob/main/docker/README.md)).
Run cloudflared as a service of the stack,
which reaches Caddy over the stack's network, and keep users you do not trust off the host.
On a host whose loopback has no IPv6, add `-f docker/ipv4-only.yml`. `WEBSPEC_HOST` must stay
an IP address (`0.0.0.0` in the stack): the init refuses a host name. Access to the Docker
daemon is access to the guard key (`docker inspect`), so the agent must not have it. On Linux,
keep the agent out of the `docker` group. With Docker Desktop on a Mac, the agent must not run
as the logged-in user.
[docker/README.md](https://github.com/I-m-A-g-I-n-E/WebSpec/blob/main/docker/README.md) covers
the rest: the key from 1Password at start (`OP_GUARD_KEY_REF`), the audit log, restarts, the
public tunnel, and running your own MCP servers.

## Egress (DP-3)

No shipped file confines the agent, so this part is yours on every platform. The allow-list is
exactly `127.0.0.1:7001` and `[::1]:7001`. Never allow `127.0.0.1:7002` (the gateway itself,
past the proxy), `:7003` (sites that bypass the gateway), or all of `127.0.0.0/8`, where any
user can listen on `127.0.0.2:7001`. On Linux, nftables can do it per user. Create the
directory (`sudo /usr/bin/install -d -m 0755 /etc/nftables.d`), and save this as
`/etc/nftables.d/webspec-agent-egress.nft`, with the agent's numeric uid (`id -u <agent>`):

```text
#!/usr/sbin/nft -f
define AGENT_UID = 1500

table inet webspec_agent_egress
delete table inet webspec_agent_egress

table inet webspec_agent_egress {
	chain output {
		# mangle (-150) runs before any DNAT in the output path, so the rule sees the
		# address the agent dialed, even where Docker rewrites a published port.
		type filter hook output priority mangle; policy accept;
		meta skuid $AGENT_UID jump agent
	}

	chain agent {
		ip daddr 127.0.0.1 tcp dport 7001 accept
		ip6 daddr ::1 tcp dport 7001 accept
		meta l4proto tcp counter reject with tcp reset
		counter reject with icmpx admin-prohibited
	}
}
```

Load it with a oneshot unit that has no `ExecStop`, so that stopping the unit never opens the
agent up:

```ini
# /etc/systemd/system/webspec-agent-egress.service
[Unit]
Description=WebSpec DP-3: confine the agent user to Caddy on 127.0.0.1:7001 and [::1]:7001
DefaultDependencies=no
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables.d/webspec-agent-egress.nft

[Install]
WantedBy=multi-user.target
```

Load the rules now, and at every boot:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now webspec-agent-egress.service
```

Then:

- **Make the agent depend on it.** Give the agent's own unit `Requires=webspec-agent-egress.service`
  and `After=webspec-agent-egress.service`, so the agent does not start when the rules fail to
  load.
- **Close the gaps around `meta skuid`.** Add `NoNewPrivileges=yes`, because a setuid program
  sends as its owner, outside the rule. Add
  `InaccessiblePaths=-/run/systemd/resolve -/run/dbus/system_bus_socket -/run/nscd -/run/avahi-daemon -/run/docker.sock`:
  resolver daemons send DNS queries as themselves, which would leave a way out. `*.localhost`
  still resolves without them where NSS resolves it in the process, through `nss-myhostname`
  (Debian: `apt install libnss-myhostname`, with `myhostname` on the `hosts` line of
  `/etc/nsswitch.conf`). Without it, the agent must dial `127.0.0.1:7001` and send the host's
  name in the `Host` header.
- **Survive an nftables reload.** If `/etc/nftables.conf` starts with `flush ruleset`, as
  Debian's does, add `include "/etc/nftables.d/*.nft"` at its end, or reloading nftables
  deletes this table while the agent runs.
- **Mind the harness's own traffic.** Calls to the model's API are egress too. If the harness
  and the agent's tools share a user, send the harness through an allow-list proxy that runs
  as another user, and add that proxy's port to the rule.
- **On a Docker host with the userland proxy turned off**, Docker does not publish
  `[::1]:7001`, and any user could listen there: delete the IPv6 line.

## Secrets, on every platform

- Keep the guard key in a password manager, as 64 hex digits (`openssl rand -hex 32`). Pipe it
  in as root. Never type it on a command line, and never put it in `config.json`, in a
  server's `args`, or in a unit or plist file: those are readable by others.
- On Linux, `gateway.env` is a systemd environment file: one `NAME=value` per line, without
  `export`. systemd ignores `export NAME=…` lines, and logs each one, value included, to the
  journal at every start. On macOS the file is read by `/bin/sh`, and `NAME='value'` is right.
- Edit secret files as root with a fixed editor, `sudo -H /usr/bin/vi <file>`. **Never use
  `sudo -e` or `sudoedit`**: it copies the file, key included, into a file of your own user,
  and opens it in your own editor, with your own environment. If the agent shares your
  account, it can read the copy.
- Refer to an MCP secret in `config.json` as `"${NAME}"`, in a stdio server's `env` or an HTTP
  server's headers. The gateway fills it from its environment when it loads the file. It never
  fills `${WEBSPEC_GUARD_KEY…}`.
- Keep `WEBSPEC_*` settings out of `gateway.env`, apart from `WEBSPEC_DOMAIN` and, on Linux,
  `WEBSPEC_GUARD_KEY`. On macOS the key belongs in `guard.key` alone: a `WEBSPEC_GUARD_KEY` in
  `gateway.env` would take its place, and `--fill-key` would then change nothing.
  Values there override the units: `WEBSPEC_AUDIT_LOG=` would turn the audit log off,
  `WEBSPEC_ACCESS_LOG=1` would log `GET` arguments, and `WEBSPEC_CORS_ORIGINS` would open the
  gateway to other origins.
- A key that ever lived in a file your login user could read is exposed. Replace it.

## The rules, platform by platform

| Rule | Linux | macOS | Docker |
|---|---|---|---|
| DP-1 dedicated user | `webspec`, a system account; the installer refuses one that can log in | `_webspec`, hidden; the installer refuses one that can log in | uid 10001 in its own container. Keep the agent off the Docker daemon |
| DP-2 non-dumpable | at startup, with `ProtectProc=invisible` | no equivalent | the gateway and its init; the init refuses a PID 1 its user can read |
| DP-3 sole egress | yours ([above](#egress-dp-3)) | yours | yours |
| DP-4 root-owned configuration | `/etc/webspec`, `/opt/webspec`; the installer refuses code others can change | the same | read-only configuration mount and root filesystems |
| DP-5 proxy | `setup-caddy.sh`: site blocks only, `421` otherwise | yours | `docker/caddy/Caddyfile`: `421` otherwise |
| DP-6 no arguments in logs | access log off; Caddy's logs filtered | the gateway's side | access log off; Caddy keeps no access log |
| DP-7 one process | one unit, restarted whatever ends it; a stop waits 40 s | one job, kept alive; a stop waits 40 s | one service, `restart: unless-stopped`; a stop waits 40 s |
| DP-8 loopback | `127.0.0.1:7002` | `127.0.0.1:7002` | `0.0.0.0:7002` inside its own network, named by `WEBSPEC_HOST` |
| DP-9 held ports | systemd holds 7001, 7003, and 7002 | launchd holds 7002 | the init holds 7002 for the container's life; Docker lets Caddy's 7001 go while that container is down |
| GD-5 guard key | `WEBSPEC_GUARD_KEY` in `gateway.env` (root only); the gateway does not start without a usable one | `guard.key`, filled with `--fill-key` | `WEBSPEC_GUARD_KEY` or `OP_GUARD_KEY_REF` at start |

## What stays your job

- **Egress (DP-3)**, on every platform.
- **A proxy on macOS** that meets DP-5 and DP-6.
- **Caddy's port on Docker** while its container is down (DP-9): run cloudflared in the stack,
  and keep users you do not trust off the host.
- **The audit log off the host**, or at least its latest hash. Every stdio server can write it.
- **Which stdio servers you run.** They share the gateway's user. Prefer HTTP MCP servers that
  run under users, or in containers, of their own.

The full list of what the reference gateway does not do yet is under
[Known limits](../spec/status.md#known-limits).
