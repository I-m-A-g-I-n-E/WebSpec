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
mail-proton.i-a-m.live → Cloudflare tunnel → webspec-gateway (localhost:7001) → mail-proton MCP server → Protonmail Bridge SMTP (localhost:1025)
```

The `mail-proton` MCP service is registered in `~/.claude.json` and auto-discovered by the webspec-gateway. It exposes three tools: `send_email`, `list_senders`, and `check_bridge`.

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

```bash
curl -X POST https://mail-proton.i-a-m.live/send_email \
  -H "Content-Type: application/json" \
  -H "X-Gimme-Definer: SEND" \
  -d '{"to":"recipient@example.com","subject":"Subject","body":"Body text"}'
```

Parameters:
- `to` (required): Recipient email address
- `subject` (required): Email subject
- `body` (required): Email body (plain text or HTML)
- `from_address` (optional): Sender address, defaults to `autodeveloper@pm.me`
- `html` (optional): Set to `true` to send HTML email

### Other Gateway Endpoints

- `GET https://mail-proton.i-a-m.live/` — List available tools
- `GET https://mail-proton.i-a-m.live/check_bridge` — Verify bridge is running
- `GET https://mail-proton.i-a-m.live/list_senders` — List allowed sender addresses

## Prerequisites

Protonmail Bridge must be running. If not:

```bash
/usr/lib/protonmail/bridge/bridge --grpc &
```

Check bridge status:
```bash
curl -s https://mail-proton.i-a-m.live/check_bridge
```

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
- Gateway registration: `~/.claude.json` → `mcpServers.mail-proton`
