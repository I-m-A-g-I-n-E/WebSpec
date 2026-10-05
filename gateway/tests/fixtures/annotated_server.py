"""A real FastMCP server (stdio) with annotated tools, for no-mock integration tests."""

from fastmcp import FastMCP

mcp = FastMCP("annotated", instructions="WebSpec integration-test server")
NOTES: dict[str, str] = {"1": "hello"}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def read_note(id: str) -> str:
    return NOTES.get(id, "")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False}, meta={"webspec/tier": "sensitive"})
def read_secret(name: str) -> str:
    return f"secret:{name}"


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
def add_note(id: str, text: str) -> str:
    NOTES[id] = text
    return "added"


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def wipe(id: str) -> str:
    NOTES.pop(id, None)
    return "wiped"


if __name__ == "__main__":
    mcp.run()
