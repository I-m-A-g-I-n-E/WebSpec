"""UFO policy: tool sensitivity tiers, run() allowlist, audit logging."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Tool sensitivity tiers
TIERS: dict[str, str] = {
    "list_vaults": "open",
    "read": "sensitive",
    "list_items": "sensitive",
    "get_item": "sensitive",
    "run": "dangerous",
}

# run() allowlist: (subcommand, allowed_first_args)
# If first_args is None, any args are allowed for that subcommand.
# If first_args is a set, only those first positional args are allowed.
RUN_ALLOWLIST: dict[str, set[str] | None] = {
    "vault": None,  # vault list, vault get, etc.
    "item": {"list", "get"},  # item list, item get only
    "document": {"get"},  # document get only
}

DEFAULT_AUDIT_PATH = Path.home() / ".webspec" / "op-auth-audit.jsonl"


def get_tier(tool_name: str) -> str:
    """Get the sensitivity tier for a tool. Unknown tools default to dangerous."""
    return TIERS.get(tool_name, "dangerous")


def is_run_allowed(subcommand: str, args: list[str] | None = None) -> bool:
    """Check if a run() subcommand + args combination is on the allowlist."""
    subcommand = subcommand.lower()
    if subcommand not in RUN_ALLOWLIST:
        return False
    allowed_first = RUN_ALLOWLIST[subcommand]
    if allowed_first is None:
        return True
    # Check the first positional arg (skip flags starting with --)
    if args:
        first_positional = next((a for a in args if not a.startswith("--")), None)
        if first_positional and first_positional.lower() in allowed_first:
            return True
    return False


def write_audit_entry(
    tool: str,
    args: dict,
    tier: str,
    provenance: str,
    outcome: str,
    clearance_valid: bool,
    log_path: Path = DEFAULT_AUDIT_PATH,
) -> None:
    """Append an audit entry to the JSONL log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "args": args,
        "tier": tier,
        "provenance": provenance,
        "outcome": outcome,
        "clearance_valid": clearance_valid,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
