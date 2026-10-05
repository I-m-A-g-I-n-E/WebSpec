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

# run() allowlist: subcommand -> allowed action (the FIRST argument, before any flag).
# Every entry is an explicit set: a None/"any" wildcard previously let `vault delete`,
# `vault edit`, and `vault user grant` through.
RUN_ALLOWLIST: dict[str, set[str]] = {
    "vault": {"list", "get"},
    "item": {"list", "get"},
    "document": {"get"},
}

# Flags that change *where* or *how* `op` acts (write files, swap config/session).
DENIED_FLAGS: set[str] = {"--out-file", "-o", "--output", "--config", "--session", "--force"}

DEFAULT_AUDIT_PATH = Path.home() / ".webspec" / "op-auth-audit.jsonl"


def get_tier(tool_name: str) -> str:
    """Get the sensitivity tier for a tool. Unknown tools default to dangerous."""
    return TIERS.get(tool_name, "dangerous")


def is_run_allowed(subcommand: str, args: list[str] | None = None) -> bool:
    """Check if a run() subcommand + args combination is on the allowlist.

    The action must be args[0] — not the first non-flag token — because a flag's
    *value* (``--vault list``) would otherwise be mistaken for the action while `op`
    parses a later token (``delete``) as the real one.
    """
    allowed_actions = RUN_ALLOWLIST.get(subcommand.lower())
    if not allowed_actions or not args:
        return False
    action = args[0]
    if action.startswith("-") or action.lower() not in allowed_actions:
        return False
    for arg in args[1:]:
        if arg.split("=", 1)[0].lower() in DENIED_FLAGS:
            return False
    return True


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
