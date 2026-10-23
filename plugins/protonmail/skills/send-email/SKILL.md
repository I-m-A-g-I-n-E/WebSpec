---
name: send-email
description: This skill should be used when the user asks to "send an email", "email someone", "send a message via proton", "mail", "send mail", "email from autodeveloper", "email from AIUnderstands", or discusses sending email through Protonmail Bridge.
---

# Send Email via Protonmail Bridge

Send emails through the local Protonmail Bridge using the `mail-proton` MCP service exposed at `mail-proton.i-a-m.live`.

## Allowed Senders

Only two addresses may send mail:
- `autodeveloper@pm.me` (default)
- `AIUnderstands@pm.me`

## How It Works

Traffic flow:
```
mail-proton.i-a-m.live → Cloudflare tunnel → Caddy (127.0.0.1:7001) → webspec-gateway (127.0.0.1:7002) → mail-proton MCP server (stdio) → Protonmail Bridge SMTP (127.0.0.1:1025)
```

The `mail-proton` MCP service is registered in the gateway's config (`mcpServers.mail-proton`) and auto-discovered by the webspec-gateway. It exposes three tools: `send_email`, `list_senders`, and `check_bridge`.

## Sending Email

### Via Python (direct SMTP — preferred for Claude Code sessions)

```python
import smtplib
from email.mime.text import MIMEText

msg = MIMEText("Your message body here")
msg['Subject'] = 'Subject line'
msg['From'] = 'autodeveloper@pm.me'  # or AIUnderstands@pm.me
msg['To'] = 'recipient@example.com'

with smtplib.SMTP('127.0.0.1', 1025) as s:
    s.starttls()
    s.login('autodeveloper@pm.me', os.environ['PROTON_BRIDGE_PASSWORD'])
    s.send_message(msg)
```

### Via WebSpec Gateway (HTTP API)

The gateway gives each tool one entry: a method, a single-segment path, and on `POST` a definer. For `mail-proton` the map is:

| Tool | Request |
|---|---|
| `send_email` | `POST /email` with `WebSpec-Definer: SEND` and a JSON body |
| `list_senders` | `GET /senders` |
| `check_bridge` | `GET /check_bridge` |

`GET /` on the destination lists these entries, and `OPTIONS /email` gives the entry's input schema and what the destination's level requires. A `POST /email` without `WebSpec-Definer` is refused with `400 missing_definer`.

The gateway serves a destination on the public domain only at level 1 or higher, so every request there also carries a fresh `WebSpec-Nonce` and a `WebSpec-Guard` tag, plus whatever else its level requires (`Idempotency-Key`, `WebSpec-Clearance`, `WebSpec-Approval`). Don't add these by hand: send the request through the reference shim (`gateway/examples/shim.py`), which holds the guard key and adds them from `OPTIONS /`. For example, `shim.call("POST", "/email", {"to": ..., "subject": ..., "body": ...}, "SEND")`. A request without the guard is refused with `401 guard_missing`.

Parameters (JSON body of `POST /email`):
- `to` (required): Recipient email address
- `subject` (required): Email subject
- `body` (required): Email body (plain text or HTML)
- `from_address` (optional): Sender address, defaults to `autodeveloper@pm.me`
- `html` (optional): Set to `true` to send HTML email

### Other Gateway Endpoints

- `GET https://mail-proton.i-a-m.live/` — List available tools and their entries
- `GET https://mail-proton.i-a-m.live/check_bridge` — Verify bridge is running
- `GET https://mail-proton.i-a-m.live/senders` — List allowed sender addresses

## Prerequisites

Protonmail Bridge must be running. If not:

```bash
/usr/lib/protonmail/bridge/bridge --grpc &
```

Check bridge status through the gateway with `GET /check_bridge`, signed by the shim as above.

## Bridge CLI Reference

```
/usr/lib/protonmail/bridge/bridge [flags]
  --grpc, -g          Start the gRPC service (headless, for server use)
  --cli, -c           Start command line interface (interactive management)
  --noninteractive, -n  Non-interactive mode
  --log-level, -l     Set log level (panic|fatal|error|warn|info|debug)
  --log-smtp          Log SMTP traffic (contains decrypted data!)
  --log-imap          Log IMAP traffic (all|client|server)
```

## Files

- MCP server: `~/MCP/mail-proton/server.py`
- Bridge binary: `/usr/lib/protonmail/bridge/bridge`
- Credentials: `~/.config/proton/proton.txt`
- Gateway registration: the gateway's config (`WEBSPEC_CONFIG`, default `~/.claude.json`) → `mcpServers.mail-proton`
