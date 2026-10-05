"""MCP server wrapping the 1Password CLI for per-secret, per-service access."""

from __future__ import annotations

import json
import subprocess

from fastmcp import FastMCP

from ufo import get_tier, is_run_allowed, write_audit_entry

OP_TIMEOUT = 10  # seconds per op call

mcp = FastMCP(
    "op-auth",
    instructions="1Password CLI proxy — read secrets, list vaults/items, run safe op commands",
)


def _run_op(*args: str, parse_json: bool = True) -> str | list | dict:
    """Run an op CLI command and return the result."""
    cmd = ["op", *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=OP_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"op exited with code {result.returncode}")

    output = result.stdout.strip()
    if parse_json and output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass
    return output


def _audit(tool: str, args: dict, outcome: str, clearance_valid: bool = True,
           provenance: str = "") -> None:
    """Log a tool call to the audit log."""
    write_audit_entry(
        tool=tool, args=args, tier=get_tier(tool),
        provenance=provenance, outcome=outcome, clearance_valid=clearance_valid,
    )


@mcp.tool()
def read(reference: str) -> str:
    """Read a single secret by its 1Password reference URI.

    Args:
        reference: 1Password secret reference (e.g. "op://Vault/Item/field").
    """
    args = {"reference": reference}
    try:
        result = _run_op("read", reference, parse_json=False)
        _audit("read", args, "allowed")
        return str(result)
    except subprocess.TimeoutExpired:
        _audit("read", args, "error")
        return "Error: op command timed out"
    except RuntimeError as e:
        _audit("read", args, "error")
        return f"Error: {e}"


@mcp.tool()
def list_vaults() -> str:
    """List all accessible 1Password vaults."""
    try:
        result = _run_op("vault", "list", "--format=json")
        _audit("list_vaults", {}, "allowed")
        return json.dumps(result, indent=2) if not isinstance(result, str) else result
    except subprocess.TimeoutExpired:
        _audit("list_vaults", {}, "error")
        return "Error: op command timed out"
    except RuntimeError as e:
        _audit("list_vaults", {}, "error")
        return f"Error: {e}"


@mcp.tool()
def list_items(vault: str) -> str:
    """List items in a specific vault.

    Args:
        vault: Vault name or ID.
    """
    args = {"vault": vault}
    try:
        result = _run_op("item", "list", "--vault", vault, "--format=json")
        _audit("list_items", args, "allowed")
        return json.dumps(result, indent=2) if not isinstance(result, str) else result
    except subprocess.TimeoutExpired:
        _audit("list_items", args, "error")
        return "Error: op command timed out"
    except RuntimeError as e:
        _audit("list_items", args, "error")
        return f"Error: {e}"


@mcp.tool()
def get_item(vault: str, item: str) -> str:
    """Get full details of a specific item.

    Args:
        vault: Vault name or ID.
        item: Item name or ID.
    """
    args = {"vault": vault, "item": item}
    try:
        result = _run_op("item", "get", item, "--vault", vault, "--format=json")
        _audit("get_item", args, "allowed")
        return json.dumps(result, indent=2) if not isinstance(result, str) else result
    except subprocess.TimeoutExpired:
        _audit("get_item", args, "error")
        return "Error: op command timed out"
    except RuntimeError as e:
        _audit("get_item", args, "error")
        return f"Error: {e}"


@mcp.tool()
def run(subcommand: str, args: list[str] | None = None) -> str:
    """Run an op CLI command (allowlisted subcommands only).

    Allowed: vault (any), item list, item get, document get.
    Everything else is denied.

    Args:
        subcommand: The op subcommand (e.g. "item", "vault", "document").
        args: Additional arguments for the subcommand.
    """
    run_args = {"subcommand": subcommand, "args": args or []}

    if not is_run_allowed(subcommand, args):
        _audit("run", run_args, "denied")
        return f"Error: '{subcommand}' with args {args or []} is not on the allowlist"

    cmd_args = [subcommand]
    if args:
        cmd_args.extend(args)
    cmd_args.append("--format=json")

    try:
        result = _run_op(*cmd_args)
        _audit("run", run_args, "allowed")
        return json.dumps(result, indent=2) if not isinstance(result, str) else result
    except subprocess.TimeoutExpired:
        _audit("run", run_args, "error")
        return "Error: op command timed out"
    except RuntimeError as e:
        _audit("run", run_args, "error")
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
