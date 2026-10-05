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

# run() allowlist: (subcommand, action) -> the ONLY flags that action may carry.
# The action must be the first argument. Every other token is either a positional
# value or exactly one of these long flags (``--flag value`` or ``--flag=value``):
# no short flags, no attached values (pflag reads ``-o/path`` as ``--out-file /path``),
# and nothing that writes files or swaps account/config/session.
RUN_ALLOWLIST: dict[tuple[str, str], set[str]] = {
    ("vault", "list"): set(),
    ("vault", "get"): set(),
    ("item", "list"): {"--vault", "--categories", "--tags"},
    ("item", "get"): {"--vault", "--fields"},
    ("document", "get"): {"--vault"},
}

DEFAULT_AUDIT_PATH = Path.home() / ".webspec" / "op-auth-audit.jsonl"


def get_tier(tool_name: str) -> str:
    """Get the sensitivity tier for a tool. Unknown tools default to dangerous."""
    return TIERS.get(tool_name, "dangerous")


def is_run_allowed(subcommand: str, args: list[str] | None = None) -> bool:
    """Check a run() subcommand + args against the per-action flag allowlist."""
    if not args:
        return False
    action = args[0]
    allowed_flags = RUN_ALLOWLIST.get((subcommand.lower(), action.lower()))
    if allowed_flags is None or action.startswith("-"):
        return False
    expect_value = False
    for arg in args[1:]:
        if expect_value:  # value of the preceding long flag
            if arg.startswith("-"):
                return False
            expect_value = False
            continue
        if not arg.startswith("-"):
            continue  # positional (item / document name)
        name, has_value, _ = arg.partition("=")
        if name not in allowed_flags:
            return False
        expect_value = not has_value
    return not expect_value


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
