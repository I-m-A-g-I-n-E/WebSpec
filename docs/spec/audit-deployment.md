# Audit and deployment

## Audit

- **AU-1** Every decision on the invocation path MUST be appended to the audit log. That
  includes calls that were invoked; requests that were denied (`405` probes, failed guards, bad
  hosts, unknown tools, and repeated query keys among them); challenges; replays; refusals by
  the server; timeouts; and errors. The reference gateway does not audit discovery (`HEAD`,
  `OPTIONS`, `GET /`), nonce issuance, `/__challenge`, the index, requests that match no route
  (among them a `CONNECT` to `host:port`, which names no path), methods it does not route
  (`TRACE`, `CONNECT`, and extension methods), which the framework answers with `405`, rejected
  CORS preflights, or errors raised outside the invocation handler.
  It does audit a request refused because the guard key is unavailable, on every path that needs
  the key (outcome `error`, status `500`, reason `guard_key_unavailable`).

- **AU-2** Each entry is one line of canonical JSON with these fields: `ts`, `service`, `host`,
  `method`, `path`, `tool`, `level`, `tier`, `risk`, `outcome`, `status`, `reason`, `definer`
  (on invoked calls), `query_sha256`, `body_sha256`, and `prev`. The URL-addressed action is
  recorded in the clear. The query and body are recorded only as SHA-256 hashes, because
  arguments may contain secrets. The hashes are unsalted, so short or guessable arguments can be
  recovered by hashing candidates. On invocations `query_sha256` hashes the canonical query; on
  denials made before the tool is resolved, it hashes the raw query.

- **AU-3** Each entry's `prev` MUST be the SHA-256 of the previous line, or 64 zeros for the
  first line. Editing or deleting a line breaks the chain from that point on, except at the end:
  removing the last lines leaves a shorter chain that still verifies, and a restarted gateway
  continues from the new tail. Keep the latest hash somewhere the agent cannot write.

- **AU-4** Rotate the log by renaming the file while the gateway runs. The chain then continues
  into the new file, whose first `prev` is the hash of the old file's last line. If the gateway
  restarts after the rename and before its next write, the new file starts again from 64 zeros.
  Truncating the file in place is indistinguishable from tampering, by design.

In the reference gateway, the log lives at `WEBSPEC_AUDIT_LOG` (default
`~/.webspec/gateway-audit.jsonl`; the production units set `/var/lib/webspec/gateway-audit.jsonl`),
is created with mode `0600`, and is turned off by an empty or blank value. Verify a file with `webspec.audit.verify_chain(path)`, where `path` is a
`pathlib.Path`. It returns the first broken line or `None`. For a rotated segment, pass the previous segment's last hash as `first_prev`.
If an entry cannot be written, the gateway logs the failure and serves the request anyway.
Failing closed at level 3 and above is [proposed](status.md#roadmap).

The `outcome` field takes one of these values: `invoked`, `denied`, `approval_challenged`,
`idempotency:{replay|mismatch|in_flight|unknown|full}`, `rejected`, `timeout`, or `error`.

## Deployment

- **DP-1** The gateway MUST run as a dedicated OS user, not as the agent's user. Processes of
  the same user can read each other's environment (`/proc/<pid>/environ` on Linux,
  `KERN_PROCARGS2` on macOS), and the guard key lives in the gateway's environment or in a file
  that its user can read. Access to a container runtime's daemon is access to the environment
  of every container it runs.

- **DP-2** On Linux, the gateway SHOULD make itself non-dumpable at startup
  (`prctl(PR_SET_DUMPABLE, 0)`). That closes `/proc/<pid>/environ`, `/proc/<pid>/mem`, and
  ptrace to processes of the same user, including the stdio MCP servers it spawns. A process
  that holds the same environment, such as a container's init, SHOULD do the same. The
  reference gateway and its Docker init do this. macOS has no equivalent, so there the
  dedicated user is the only boundary.

- **DP-3** The gateway SHOULD be the agent's only network egress, enforced at the network
  layer by a firewall, a network namespace, or an egress-proxy allow-list. A chokepoint only
  holds if nothing routes around it: the allow-list names only the proxy's listener for the
  gateway, and a route that bypasses the gateway, such as a site proxied straight to a local
  app, is served on a listener outside it.

- **DP-4** The agent MUST NOT be able to write the gateway's configuration, because whoever can
  edit it can add services, change levels, and write overrides. The agent SHOULD NOT be able to
  write the audit log, or the log SHOULD be shipped off the host.

- **DP-5** A proxy in front of the gateway MUST forward only WebSpec hosts: destinations under
  the public domain, and loopback names (`*.localhost`) for connections from the host itself,
  never for requests that came in through the public path. It MUST refuse every other host, and
  MUST preserve the `Host` header, which the guard signs. The gateway treats a request whose
  host is a loopback name as local, so a loopback name that came in from outside would reach
  level-0 destinations without the guard. The reference proxy answers `421` in both cases.

- **DP-6** `GET` arguments travel in URLs, and the guard, nonce, clearance, and approval travel
  in headers. The gateway's own access log is therefore off by default (`WEBSPEC_ACCESS_LOG=1`
  turns it on), and proxies SHOULD keep query strings, request headers, and the userinfo of
  absolute-form request targets out of their logs.

- **DP-7** Run a single gateway process. Nonces, contract pins, clearances, approvals, and
  idempotency records are held in memory, per process, and a restart forgets them. The
  reference units restart the gateway whatever ends it, so a stdio server, which runs as the
  gateway's user, can force such a restart. A service manager SHOULD stop the gateway by
  signalling it alone and SHOULD wait longer than its graceful shutdown (35 s in the reference
  gateway) before killing it. A call cut midway may already have run its tool, and the
  restarted gateway no longer knows its idempotency key.

- **DP-8** Unless `WEBSPEC_CORS_ORIGINS` lists an origin, a page from another origin cannot read
  the gateway's responses or send it unsafe methods. Its simple `GET`s still reach read-only
  tools on level-0 destinations, though it cannot read the answers. The gateway binds to loopback
  (`127.0.0.1`) unless `WEBSPEC_HOST` says otherwise; an empty value counts as unset. A
  listening socket handed to the gateway (DP-9) is used only if it is bound to loopback or to
  exactly the address `WEBSPEC_HOST` names, which must then be an IP address, not a name.

- **DP-9** The listening ports of the proxy and the gateway SHOULD be held by something that
  outlives their processes, such as systemd or launchd socket activation or a container's init,
  so that they stay bound while the processes restart. A port that is free between one process
  and the next can be taken by any local process, which then receives full URLs, bodies, and
  the signed headers. A container's init holds a port in the container's own network namespace,
  for the container's life; a port that the container runtime publishes on the host is the
  runtime's, which frees it while the container is down. A gateway that was asked to take a
  socket MUST NOT fall back to binding the port itself.

In the reference deployment (`gateway/deploy/linux` and `gateway/tools/setup-caddy.sh`), a
Cloudflare tunnel sends `*.{domain}` to Caddy on `127.0.0.1:7001`. Caddy listens there and on
`[::1]:7001`, and forwards to the gateway on `127.0.0.1:7002`. Sites that bypass the gateway are served on
`:7003`. systemd holds every one of these ports. [Deploying](../guide/deploy.md) covers Linux,
macOS, and Docker.

## Configuration

The gateway reads MCP server definitions from a JSON file (`WEBSPEC_CONFIG`, by default
`~/.claude.json`) under `mcpServers`. It uses the same entries an MCP client uses, plus four
WebSpec keys:

```json
{
  "mcpServers": {
    "mail": {
      "command": "python",
      "args": ["/opt/webspec/services/mail-proton/server.py"],
      "level": 4,
      "labels": ["eu"],
      "tools": {
        "list_senders": {"read_only": true, "open_world": false}
      }
    },
    "tickets": {
      "type": "http",
      "url": "https://mcp.tickets.example/mcp",
      "headers": {"Authorization": "Bearer ${TICKETS_TOKEN}"},
      "level": 3
    }
  }
}
```

| Key | Meaning |
|---|---|
| `level` | Security level 0–4 ([LV-1](levels.md)) |
| `guard` | Legacy switch. `true` means at least level 1 |
| `labels` | Allowed qualifier labels, in canonical order ([HG-3](addressing.md#host)) |
| `tools` | Operator overrides per tool ([TC-5](methods.md#tool-contracts)) |

The file is checked every 30 seconds and reloaded when it changes: a new file, or a new size,
modification time, or change time. A version that cannot be loaded is rejected as a whole: a file
that cannot be read, malformed JSON, or JSON that is not a registry (a top level, `mcpServers`, or
entry that is not an object, an `http` entry without `url`, or headers that are not strings). The
gateway then logs one warning and keeps the last good registry, which at startup is empty. `${VAR}` in HTTP headers and in a stdio server's `env`
values is filled from the gateway's environment, except that a variable whose name starts with
`WEBSPEC_GUARD_KEY` is never expanded.

Validation is shallow, so check your entries:

- `type` is `http` or omitted (stdio). Any other value, such as `sse`, is treated as stdio.
- `"level": null` counts as absent.
- Two servers whose names normalize to the same destination overwrite each other; the last one
  wins.
- Changes to an existing server's command, URL, or headers apply when the gateway next connects
  to it.
- An unresolved `${VAR}` is sent literally.

| Environment variable | Purpose |
|---|---|
| `WEBSPEC_GUARD_KEY` | Guard key, as 64 hex digits or a passphrase. Required unless `WEBSPEC_GUARD_KEY_FILE` is set; a blank value counts as unset |
| `WEBSPEC_GUARD_KEY_FILE` | File that holds the guard key, read on every request ([GD-5](levels.md#level-1-signed)) |
| `WEBSPEC_REQUIRE_GUARD_KEY` | Any value but empty or `0` makes a usable key in `WEBSPEC_GUARD_KEY` itself a condition of starting: without one the gateway exits with status 3 before it serves, and neither the key file nor the development key counts. The Linux unit sets it |
| `WEBSPEC_GUARD_KEY_DEV_EPHEMERAL` | `1` mints a throwaway in-memory key, for local development only |
| `WEBSPEC_CONFIG` | Path of the configuration file |
| `WEBSPEC_DOMAIN` | Public domain (enables `{destination}.{domain}` routing) |
| `WEBSPEC_HOST`, `WEBSPEC_PORT`, `WEBSPEC_INTERNAL_PORT` | Bind address (default `127.0.0.1`; empty counts as unset; a socket passed in that is not on loopback must be on exactly this address, given as an IP address, DP-8), public port (default 7001), and listen port behind a proxy |
| `LISTEN_PID`, `LISTEN_FDS` | A listening socket passed by systemd or another parent to the process that `LISTEN_PID` names, as `sd_listen_fds(3)` describes (DP-9) |
| `WEBSPEC_LAUNCHD_SOCKET` | The name of the launchd socket to serve on (macOS, DP-9) |
| `WEBSPEC_AUDIT_LOG` | Audit log path. An empty value disables the log |
| `WEBSPEC_APPROVERS_FILE` | OpenSSH allowed-signers file for level-4 approvals |
| `WEBSPEC_SSH_KEYGEN` | The `ssh-keygen` used to verify approvals |
| `WEBSPEC_APPROVER_KEY`, `WEBSPEC_APPROVAL_SIGNER` | Signing key and signer for `webspec-ctl approve` |
| `WEBSPEC_CORS_ORIGINS` | Comma-separated origins allowed cross-origin |
| `WEBSPEC_ACCESS_LOG` | `1` enables the HTTP access log |
| `WEBSPEC_LOG_LEVEL` | Log level (default `info`) |
