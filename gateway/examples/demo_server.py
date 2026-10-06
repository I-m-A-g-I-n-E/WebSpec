"""A tiny MCP server for the WebSpec guide: one read, one send, one delete.

Nothing leaves the machine — ``send_note`` only appends to an in-memory outbox — but its
annotations say what a real sending tool would say (it reaches the open world), so the
gateway treats it like one.
"""

from fastmcp import FastMCP

mcp = FastMCP("notes", instructions="WebSpec guide demo server")
NOTES: dict[str, str] = {"welcome": "Hello from WebSpec."}
OUTBOX: list[dict[str, str]] = []


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def read_note(id: str) -> str:
    """Read a note."""
    return NOTES.get(id, "")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
def send_note(id: str, to: str) -> dict:
    """Send a note to someone (simulated: queued in an in-memory outbox)."""
    OUTBOX.append({"to": to, "text": NOTES.get(id, "")})
    return {"queued": len(OUTBOX), "to": to}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True,
                       "openWorldHint": False})
def delete_note(id: str) -> str:
    """Delete a note."""
    return "deleted" if NOTES.pop(id, None) is not None else "absent"


if __name__ == "__main__":
    mcp.run()
