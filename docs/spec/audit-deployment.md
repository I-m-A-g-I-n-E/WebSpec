# Audit, deployment, and configuration

The gateway keeps a hash-chained log of the decisions it makes on tool calls ([Audit](#audit)).
The [deployment](#deployment) rules say how to run it so that the agent cannot read its key,
change its configuration, take over its ports, or route around it.
[Configuration](#configuration) lists the configuration file's keys and the gateway's
environment variables.

## Audit

- **AU-1** Every decision on the invocation path MUST be appended to the audit log. That
  includes calls that were invoked; requests that were denied (`405` probes, failed guards, bad
  hosts, unknown tools, and repeated query keys among them); approval challenges (`428`);
  replays; refusals by the server; timeouts; and errors.

    The reference gateway does not audit:

    - discovery (`HEAD`, `OPTIONS`, `GET /`), nonce issuance, or the retired `/__challenge`
    - the list of destinations (`GET /` on the bare domain)
    - requests that match no route, among them a `CONNECT` to `host:port`, which names no path
    - methods it does not route (`TRACE`, `CONNECT`, and extension methods), which the web
      framework answers with `405`
    - rejected CORS preflights
    - errors raised outside the invocation handler

    It does audit a request refused because the guard key is unavailable, on every path that
    needs the key (outcome `error`, status `500`, reason `guard_key_unavailable`).

- **AU-2** Each entry is one line of canonical JSON with these fields: `ts`, `service`, `host`,
  `method`, `path`, `tool`, `level`, `tier`, `risk`, `outcome`, `status`, `reason`, `definer`
  (on invoked calls whose definer was checked), `query_sha256`, `body_sha256`, and `prev`. The
  method, host, path, and tool are recorded in the clear. The query and body are recorded only
  as SHA-256 hashes, because arguments may contain secrets. The hashes are unsalted, so short or
  guessable arguments can be recovered by hashing candidates. Once the tool is resolved,
  `query_sha256` hashes the canonical query ([GD-3](levels.md#level-1-signed)); before that,
  for example for a failed guard or an unknown tool, it hashes the raw query.

- **AU-3** Each entry's `prev` MUST be the SHA-256 of the previous line, or 64 zeros for the
  first line. Editing or deleting a line breaks the chain from that point on, except at the end:
  removing the last lines leaves a shorter chain that still verifies, and a restarted gateway
  continues from the new tail. Keep the latest hash somewhere the agent cannot write.

- **AU-4** Rotate the log by renaming the file while the gateway runs. The chain then continues
  into the new file, whose first `prev` is the hash of the old file's last line. If the gateway
  restarts after the rename and before its next write, the new file starts again from 64 zeros.
  Truncating the file in place is indistinguishable from tampering, by design.

- **AU-5** The gateway MUST also append an entry when a rebuild first finds a pinned key
  resolving to a different tool, and another when a `bindings` entry confirms that change and
  the key is pinned again ([PA-8](addressing.md#building-the-map)). Such an entry has the fields
  of AU-2 that apply to it. Its `method`, `path`, and for a `POST` key `definer` give the key.
  `tool`, `tier`, and `risk` describe the tool the key now resolves to, and the extra fields
  `pinned_tool`, `pinned_tier`, and `pinned_risk` describe the tool it was pinned to.

The `outcome` field says what the gateway decided:

| `outcome` | Meaning |
|---|---|
| `invoked` | The gateway called the tool and got an answer |
| `denied` | The gateway refused the request without calling the tool |
| `approval_challenged` | The gateway answered `428` with a level-4 approval challenge |
| `idempotency:{kind}` | The `Idempotency-Key` decided the answer, and the tool was not called. `{kind}` is `replay`, `mismatch`, `in_flight`, `unknown`, or `full` ([Level 2](levels.md#level-2-bound)) |
| `binding_changed` | A rebuild found a pinned key resolving to a different tool (AU-5) |
| `binding_confirmed` | A `bindings` entry confirmed that change, and the key was pinned again (AU-5) |
| `rejected` | The MCP server refused the call (`502 tool_rejected`) |
| `timeout` | The MCP server did not answer in time (`504 tool_timeout`) |
| `error` | Anything else failed, such as an unavailable guard key (`500 guard_key_unavailable`) or a broken connection (`503`) |

`tier` and `risk` come from the tool's [contract](methods.md#tool-contracts). `risk` is 0 for a
read-only tool, 1 for one that only adds, and 2 for one that may destroy or overwrite.

In the reference gateway:

- **Writes fail open.** If an entry cannot be written, the gateway logs the failure and serves
  the request anyway. Failing closed at level 3 and above is [proposed](status.md#roadmap).
- **Location.** The log lives at `WEBSPEC_AUDIT_LOG` (default `~/.webspec/gateway-audit.jsonl`;
  the production units set `/var/lib/webspec/gateway-audit.jsonl`). It is created with mode
  `0600`, and an empty or blank value turns it off.
- **Verification.** `webspec.audit.verify_chain(path)`, where `path` is a `pathlib.Path`,
  returns the number of the first broken line, counting from 1, or `None` if the chain holds.
  For the file that follows a rotation, pass the SHA-256 of the previous file's last line as
  `first_prev`. [Deploying](../guide/deploy.md#day-to-day) has the commands.

## Deployment

- **DP-1** The gateway MUST run as a dedicated OS user, not as the agent's user. Processes of
  the same user can read each other's environment (`/proc/<pid>/environ` on Linux,
  `KERN_PROCARGS2` on macOS), and the guard key lives in the gateway's environment or in a file
  that its user can read. Access to a container runtime's daemon is access to the environment
  of every container it runs.

- **DP-2** On Linux, the gateway SHOULD make itself non-dumpable at startup
  (`prctl(PR_SET_DUMPABLE, 0)`). That closes its `/proc/<pid>/environ` and `/proc/<pid>/mem`,
  and ptrace of it, to other processes of the same user, the stdio MCP servers it spawns among
  them. The servers themselves stay readable to one another: a process is dumpable again after
  `exec`. A process that holds the same environment, such as a container's init, SHOULD do the
  same. The reference gateway and its Docker init do this. macOS has no equivalent, so there
  the dedicated user is the only boundary.

- **DP-3** The gateway SHOULD be the agent's only network egress, enforced at the network
  layer by a firewall, a network namespace, or an egress-proxy allow-list. A chokepoint holds
  only if nothing routes around it. The allow-list therefore names only the proxy listener that
  leads to the gateway, and a site that bypasses the gateway, such as one proxied straight to a
  local app, is served on another listener, which the allow-list leaves out.

- **DP-4** The agent MUST NOT be able to write the gateway's configuration, because whoever can
  edit it can add services, change levels, and write overrides. The agent SHOULD NOT be able to
  write the audit log, or the log SHOULD be shipped off the host.

- **DP-5** A proxy in front of the gateway MUST forward only WebSpec hosts, MUST refuse every
  other host, and MUST preserve the `Host` header, which the guard signs. The WebSpec hosts are
  the destinations under the public domain and, on connections from the host itself only,
  loopback names (`*.localhost`). A loopback name that came in through the public path is not a
  WebSpec host: the gateway treats a request whose host is a loopback name as local, so it would
  reach level-0 destinations without the guard. The reference proxy answers `421` to every host
  it refuses.

- **DP-6** `GET` arguments travel in URLs, and the guard, nonce, clearance, and approval travel
  in headers. The gateway's own access log is therefore off by default (`WEBSPEC_ACCESS_LOG=1`
  turns it on), and proxies SHOULD keep query strings, request headers, and any credentials
  (`user:pass@`) in the request line out of their logs.

- **DP-7** Run a single gateway process. Nonces, contract pins, binding pins, clearances,
  approvals, and idempotency records are held in memory, per process, and a restart forgets them
  ([Known limits](status.md#known-limits)). A service manager SHOULD stop the gateway by
  signalling the gateway process alone, not the stdio servers it started, and SHOULD wait
  longer than its graceful shutdown (35 s in the reference gateway) before killing it: a call
  cut midway may already have run its tool, and the restarted gateway no longer knows its
  idempotency key.

- **DP-8** The gateway binds to loopback (`127.0.0.1`) unless `WEBSPEC_HOST` says otherwise; an
  empty value counts as unset. A listening socket handed to the gateway (DP-9) is used only if
  it is bound to loopback or to exactly the address `WEBSPEC_HOST` names, which must then be an
  IP address, not a name. Unless `WEBSPEC_CORS_ORIGINS` lists an origin, a page from another
  origin cannot read the gateway's responses, and it cannot send anything that needs a CORS
  preflight, because the gateway refuses the preflight. `PUT`, `PATCH`, and `DELETE` need one by
  themselves. `POST` is the only unsafe method a page can send without one, and a `POST` must
  carry the definer, a header that needs one ([DF-1](methods.md#per-method-rules)). A form it
  posts therefore arrives without a definer and is refused. Its plain `GET`s still reach
  read-only tools on level-0 destinations, though it cannot read the answers. When
  `WEBSPEC_CORS_ORIGINS` lists an origin, the gateway MUST let that origin send
  `WebSpec-Definer`, `WebSpec-Guard`, `WebSpec-Nonce`, `WebSpec-Clearance`, `WebSpec-Approval`,
  `Idempotency-Key`, and `Content-Type` (`Access-Control-Allow-Headers`), and MUST let it read
  `Allow`, `Link`, `WebSpec-Level`, `WebSpec-Tier`, `WebSpec-Taint`, `WebSpec-Status`,
  `WebSpec-Tool-Count`, `WebSpec-Service`, and `Idempotent-Replayed`
  (`Access-Control-Expose-Headers`). Otherwise a harness in that origin could not see the taint
  mark ([RS-2](methods.md#responses)).

- **DP-9** The listening ports of the proxy and the gateway SHOULD be held by something that
  outlives their processes, such as systemd or launchd socket activation or a container's init,
  so that they stay bound while the processes restart. A port that is free between one process
  and the next can be taken by any local process, which then receives full URLs, bodies, and
  the signed headers. A container's init holds a port in the container's own network namespace,
  for the container's life; a port that the container runtime publishes on the host is the
  runtime's, which frees it while the container is down. A gateway that was asked to take a
  socket MUST NOT fall back to binding the port itself.

[Deploying](../guide/deploy.md#the-rules-platform-by-platform) shows how the reference
deployments for Linux, macOS, and Docker meet each of these rules, and which ones they leave to
you.

## Configuration

The gateway reads MCP server definitions from a JSON file (`WEBSPEC_CONFIG`, by default
`~/.claude.json`) under `mcpServers`. It uses the same entries an MCP client uses, plus five
WebSpec keys:

```json
{
  "mcpServers": {
    "mail": {
      "command": "/opt/webspec/venv/bin/python",
      "args": ["/opt/webspec/services/mail-proton/server.py"],
      "env": {"PROTON_BRIDGE_PASSWORD": "${PROTON_BRIDGE_PASSWORD}"},
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
| `labels` | Allowed qualifier labels, in canonical order ([HG-3](addressing.md#host)). The reference gateway refuses hosts that carry them ([HG-5](addressing.md#host)) |
| `tools` | Operator overrides per tool ([TC-5](methods.md#tool-contracts)) |
| `bindings` | Operator confirmations of changed route keys, as `{"METHOD /path[ DEFINER]": "tool"}` ([PA-8](addressing.md#building-the-map)) |

`${VAR}` in HTTP headers and in a stdio server's `env` values is filled from the gateway's
environment when the file is loaded. An unresolved `${VAR}` is sent literally, and a variable
whose name starts with `WEBSPEC_GUARD_KEY` is never expanded.

The file is checked every 30 seconds and reloaded when it changes: a new file, or a new size,
modification time, or change time. A version that cannot be loaded is rejected as a whole: a file
that cannot be read, malformed JSON, or JSON that is not a registry (a top level, `mcpServers`, or
entry that is not an object, an `http` entry without `url`, or headers that are not strings). The
gateway then logs one warning and keeps the last good registry, which at startup is empty. A
change to an existing server's command, `args`, `env`, URL, or headers takes effect only when
the gateway next connects to it, after its connection breaks or a call times out. Restart the
gateway to apply such a change at once.

Validation is shallow, so check your entries:

- `type` is `http` or omitted (stdio). Any other value, such as `sse`, is treated as stdio.
- `"level": null` counts as absent.
- Two servers whose names normalize to the same destination overwrite each other; the last one
  wins.

| Environment variable | Purpose |
|---|---|
| `WEBSPEC_GUARD_KEY` | Guard key, as 64 hex digits or a passphrase. Required unless `WEBSPEC_GUARD_KEY_FILE` is set; a blank value counts as unset |
| `WEBSPEC_GUARD_KEY_FILE` | File that holds the guard key, read on every request ([GD-5](levels.md#level-1-signed)) |
| `WEBSPEC_REQUIRE_GUARD_KEY` | Any value but empty or `0` makes a usable key in `WEBSPEC_GUARD_KEY` itself a condition of starting: without one the gateway exits with status 3 before it serves, and neither the key file nor the development key counts. The Linux unit sets it |
| `WEBSPEC_GUARD_KEY_DEV_EPHEMERAL` | `1` mints a throwaway in-memory key, for local development only |
| `WEBSPEC_CONFIG` | Path of the configuration file |
| `WEBSPEC_DOMAIN` | Public domain (enables `{destination}.{domain}` routing) |
| `WEBSPEC_HOST` | Bind address, default `127.0.0.1` ([DP-8](#deployment)) |
| `WEBSPEC_PORT` | Public port (default 7001). The gateway also listens on it when `WEBSPEC_INTERNAL_PORT` is unset |
| `WEBSPEC_INTERNAL_PORT` | Listen port behind a proxy (7002 in the reference deployments) |
| `LISTEN_PID`, `LISTEN_FDS` | A listening socket passed by systemd or another parent to the process that `LISTEN_PID` names, as `sd_listen_fds(3)` describes (DP-9) |
| `WEBSPEC_LAUNCHD_SOCKET` | The name of the launchd socket to serve on (macOS, DP-9) |
| `WEBSPEC_AUDIT_LOG` | Audit log path ([Audit](#audit)) |
| `WEBSPEC_APPROVERS_FILE` | OpenSSH allowed-signers file for level-4 approvals |
| `WEBSPEC_SSH_KEYGEN` | The `ssh-keygen` used to verify approvals |
| `WEBSPEC_APPROVER_KEY`, `WEBSPEC_APPROVAL_SIGNER` | Signing key and signer for `webspec-ctl approve` |
| `WEBSPEC_CORS_ORIGINS` | Comma-separated origins allowed cross-origin |
| `WEBSPEC_ACCESS_LOG` | `1` enables the HTTP access log |
| `WEBSPEC_LOG_LEVEL` | Log level (default `info`) |
