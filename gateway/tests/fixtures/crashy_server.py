"""A real FastMCP server whose ``send_and_crash`` records that it ran, then kills its own
process mid-call. The gateway must report the outcome as unknown, keep the idempotency key
from being reused, and reconnect for the next call."""

import os

from fastmcp import FastMCP

mcp = FastMCP("crashy", instructions="WebSpec crash-recovery test server")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
def send_and_crash(to: str) -> str:
    with open(os.environ["CRASHY_MARKER"], "a") as f:
        f.write(to + "\n")
    os._exit(1)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def alive() -> str:
    return "yes"


if __name__ == "__main__":
    mcp.run()
