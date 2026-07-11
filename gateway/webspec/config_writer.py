"""Safe read-modify-write for ~/.claude.json mcpServers and ~/.env."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


_CLAUDE_CONFIG = Path.home() / ".claude.json"
_ENV_FILE = Path.home() / ".env"


def _read_claude_config(path: Path | None = None) -> dict:
    """Read and parse ~/.claude.json."""
    p = path or _CLAUDE_CONFIG
    with open(p) as f:
        return json.load(f)


def _write_claude_config(data: dict, path: Path | None = None) -> None:
    """Atomic write to ~/.claude.json with backup."""
    p = path or _CLAUDE_CONFIG
    tmp = p.with_suffix(".json.tmp")
    bak = p.with_suffix(".json.bak")

    # Validate before writing
    payload = json.dumps(data, indent=2) + "\n"
    json.loads(payload)  # round-trip validation

    # Backup existing
    if p.exists():
        bak.write_text(p.read_text())

    # Atomic write
    tmp.write_text(payload)
    os.replace(tmp, p)


def add_service(name: str, entry: dict, path: Path | None = None) -> None:
    """Add or update a service in ~/.claude.json mcpServers.

    Idempotent: adding an existing service updates it.
    """
    data = _read_claude_config(path)
    data.setdefault("mcpServers", {})[name] = entry
    _write_claude_config(data, path)


def remove_service(name: str, path: Path | None = None) -> None:
    """Remove a service from ~/.claude.json mcpServers.

    Idempotent: removing a missing service is a no-op.
    """
    data = _read_claude_config(path)
    servers = data.get("mcpServers", {})
    if name in servers:
        del servers[name]
        _write_claude_config(data, path)


def list_services(path: Path | None = None) -> dict[str, dict]:
    """Return current mcpServers from ~/.claude.json."""
    data = _read_claude_config(path)
    return data.get("mcpServers", {})


def add_env_var(key: str, value: str = "", path: Path | None = None) -> None:
    """Add a key=value line to ~/.env if not already present.

    If key exists, does not overwrite (idempotent).
    """
    p = path or _ENV_FILE
    lines = p.read_text().splitlines() if p.exists() else []

    # Check if key already present
    pattern = re.compile(rf"^{re.escape(key)}=")
    for line in lines:
        if pattern.match(line):
            return  # already exists

    lines.append(f"{key}={value}")
    p.write_text("\n".join(lines) + "\n")


def remove_env_var(key: str, path: Path | None = None) -> None:
    """Remove a key from ~/.env.

    Idempotent: removing a missing key is a no-op.
    """
    p = path or _ENV_FILE
    if not p.exists():
        return

    pattern = re.compile(rf"^{re.escape(key)}=")
    lines = p.read_text().splitlines()
    filtered = [line for line in lines if not pattern.match(line)]

    if len(filtered) != len(lines):
        p.write_text("\n".join(filtered) + "\n")
