"""A real FastMCP server (stdio) that reports the environment it was started with."""

import os

from fastmcp import FastMCP

mcp = FastMCP("envprobe", instructions="WebSpec environment test server")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def getenv(name: str) -> str:
    """The value of an environment variable, or <unset>."""
    return os.environ.get(name, "<unset>")


if __name__ == "__main__":
    mcp.run()
