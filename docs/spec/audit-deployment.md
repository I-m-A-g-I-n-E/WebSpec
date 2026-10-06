# Audit and deployment

## Audit

- **AU-1** Every decision MUST be appended to the audit log. That includes calls that were
  invoked; requests that were denied (`405` probes, failed guards, bad hosts, unknown tools, and
  repeated query keys among them); challenges; replays; refusals by the server; timeouts; and
  errors.
- **AU-2** Each entry is one line of canonical JSON with these fields: `ts`, `service`, `host`,
  `method`, `path`, `tool`, `level`, `tier`, `risk`, `outcome`, `status`, `reason`, `definer`
  (on invoked calls), `query_sha256`, `body_sha256`, and `prev`. The URL-addressed action is
  recorded in the clear. The query and body are recorded only as SHA-256 hashes, because
  arguments may contain secrets.
- **AU-3** Each entry's `prev` MUST be the SHA-256 of the previous line, or 64 zeros for the
  first line. Editing or deleting any line breaks the chain from that point on.
- **AU-4** Rotate the log by renaming the file. The chain then continues into the new file,
  whose first `prev` is the hash of the old file's last line. Truncating the file in place is
  indistinguishable from tampering, by design.

In the reference gateway, the log lives at `WEBSPEC_AUDIT_LOG` (default
`~/.webspec/gateway-audit.jsonl`), is created with mode `0600`, and is turned off by an empty
value. Verify a file with `webspec.audit.verify_chain(path)`, which returns the first broken
line or `None`. For a rotated segment, pass the previous segment's last hash as `first_prev`.
If an entry cannot be written, the gateway logs the failure and serves the request anyway.
Failing closed at level 3 and above is [proposed](status.md#roadmap).

The `outcome` field takes one of these values: `invoked`, `denied`, `approval_challenged`,
`idempotency:{replay|mismatch|in_flight|unknown|full}`, `rejected`, `timeout`, or `error`.

## Deployment

- **DP-1** The gateway MUST run as a dedicated OS user, not as the agent's user. Processes of
  the same user can read each other's environment (`/proc/<pid>/environ` on Linux,
  `KERN_PROCARGS2` on macOS), and the guard key lives in the gateway's environment.
- **DP-2** On Linux, the gateway SHOULD make itself non-dumpable at startup
  (`prctl(PR_SET_DUMPABLE, 0)`). That closes `/proc/<pid>/environ`, `/proc/<pid>/mem`, and
  ptrace to processes of the same user, including the stdio MCP servers it spawns. The
  reference gateway does this.
- **DP-3** The gateway SHOULD be the agent's only network egress, enforced at the network
  layer by a firewall, a network namespace, or an egress-proxy allow-list. A chokepoint only
  holds if nothing routes around it.
- **DP-4** The agent MUST NOT be able to write the gateway's configuration, because whoever can
  edit it can add services, change levels, and write overrides. The agent SHOULD NOT be able to
  write the audit log, or the log SHOULD be shipped off the host.
- **DP-5** A proxy in front of the gateway MUST forward only hosts under the public domain, and
  MUST preserve the `Host` header, which the guard signs. The gateway treats a request whose
  host is a loopback name (`*.localhost`) as local.
- **DP-6** `GET` arguments travel in URLs. The gateway's own access log is therefore off by
  default (`WEBSPEC_ACCESS_LOG=1` turns it on), and proxies SHOULD drop or redact query strings
  from their logs.
- **DP-7** Run a single gateway process. Nonces, contract pins, clearances, approvals, and
  idempotency records are held in memory, per process.
- **DP-8** The gateway allows no cross-origin browser access unless `WEBSPEC_CORS_ORIGINS`
  lists the origins to allow. It binds to loopback (`127.0.0.1`) unless `WEBSPEC_HOST` says
  otherwise.

The reference deployment runs `*.i-a-m.live` through a Cloudflare tunnel to Caddy on `:7001`,
which forwards to the gateway on `:7002`.

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

The file is checked every 30 seconds. If a new version fails to parse, the gateway keeps the
last good registry. `${VAR}` in HTTP headers is filled from the environment.

| Environment variable | Purpose |
|---|---|
| `WEBSPEC_GUARD_KEY` | Guard key (required), as 64 hex digits or a passphrase |
| `WEBSPEC_GUARD_KEY_DEV_EPHEMERAL` | `1` mints a throwaway in-memory key, for local development only |
| `WEBSPEC_CONFIG` | Path of the configuration file |
| `WEBSPEC_DOMAIN` | Public domain (enables `{destination}.{domain}` routing) |
| `WEBSPEC_HOST`, `WEBSPEC_PORT`, `WEBSPEC_INTERNAL_PORT` | Bind address, public port (default 7001), and listen port behind a proxy |
| `WEBSPEC_AUDIT_LOG` | Audit log path. An empty value disables the log |
| `WEBSPEC_APPROVERS_FILE` | OpenSSH allowed-signers file for level-4 approvals |
| `WEBSPEC_SSH_KEYGEN` | The `ssh-keygen` used to verify approvals |
| `WEBSPEC_APPROVER_KEY`, `WEBSPEC_APPROVAL_SIGNER` | Signing key and signer for `webspec-ctl approve` |
| `WEBSPEC_CORS_ORIGINS` | Comma-separated origins allowed cross-origin |
| `WEBSPEC_ACCESS_LOG` | `1` enables the HTTP access log |
| `WEBSPEC_LOG_LEVEL` | Log level (default `info`) |
