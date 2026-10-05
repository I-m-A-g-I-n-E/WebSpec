"""MCP server wrapping Protonmail Bridge SMTP for sending email."""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastmcp import FastMCP

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 1025

# Only these addresses may send mail.
ALLOWED_SENDERS = {
    "autodeveloper@pm.me",
    "AIUnderstands@pm.me",
}

BRIDGE_PASSWORD = os.environ.get("PROTON_BRIDGE_PASSWORD", "")

mcp = FastMCP(
    "mail-proton",
    instructions="Send email through Protonmail Bridge",
)


@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    from_address: str = "autodeveloper@pm.me",
    html: bool = False,
) -> str:
    """Send an email via Protonmail Bridge.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Email body (plain text or HTML).
        from_address: Sender address. Must be autodeveloper@pm.me or AIUnderstands@pm.me.
        html: If True, send body as HTML. Otherwise plain text.
    """
    if from_address not in ALLOWED_SENDERS:
        return f"Error: '{from_address}' not allowed. Use one of: {', '.join(sorted(ALLOWED_SENDERS))}"

    msg = MIMEMultipart("alternative") if html else MIMEMultipart()
    msg["From"] = from_address
    msg["To"] = to
    msg["Subject"] = subject

    if html:
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(body, "html"))
    else:
        msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(BRIDGE_HOST, BRIDGE_PORT) as server:
            server.starttls()
            server.login(from_address, BRIDGE_PASSWORD)
            server.send_message(msg)
    except ConnectionRefusedError:
        return "Error: Protonmail Bridge not running. Start it with: /usr/lib/protonmail/bridge/bridge --grpc &"
    except smtplib.SMTPAuthenticationError as exc:
        return f"Error: SMTP auth failed — {exc}"
    except Exception as exc:
        return f"Error: {exc}"

    return f"Sent to {to} from {from_address}"


@mcp.tool()
def list_senders() -> list[str]:
    """List the allowed sender addresses."""
    return sorted(ALLOWED_SENDERS)


@mcp.tool()
def check_bridge() -> str:
    """Check if Protonmail Bridge SMTP is reachable."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect((BRIDGE_HOST, BRIDGE_PORT))
        sock.close()
        return "Bridge is running (SMTP port 1025 open)"
    except (ConnectionRefusedError, socket.timeout):
        return "Bridge is NOT running. Start it with: /usr/lib/protonmail/bridge/bridge --grpc &"


if __name__ == "__main__":
    mcp.run()
