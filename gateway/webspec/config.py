"""Service registry: parse ~/.claude.json mcpServers and manage session keys."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ServiceEntry:
    name: str  # normalized subdomain label
    original_name: str  # raw key from config
    transport_type: Literal["stdio", "http"]
    # stdio fields
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # http fields
    url: str | None = None
    # guard: require HMAC + nonce authentication
    guard: bool = False


def normalize_name(raw: str) -> str:
    """Normalize an MCP server name to a valid subdomain label.

    NanoBanana[id=4SNMTV] → nanobanana
    MCP_DOCKER → mcp-docker
    iTerm → iterm
    Notion[id=VABYJJ] → notion
    """
    # Strip bracketed suffixes like [id=4SNMTV]
    name = re.sub(r"\[.*?\]", "", raw).strip()
    # Lowercase
    name = name.lower()
    # Replace underscores and spaces with hyphens
    name = re.sub(r"[_\s]+", "-", name)
    # Remove anything that isn't alphanumeric or hyphen
    name = re.sub(r"[^a-z0-9-]", "", name)
    # Collapse multiple hyphens
    name = re.sub(r"-+", "-", name)
    # Strip leading/trailing hyphens
    name = name.strip("-")
    return name


def parse_claude_config(path: Path | None = None) -> dict[str, ServiceEntry]:
    """Parse mcpServers from ~/.claude.json, return {normalized_name: ServiceEntry}."""
    if path is None:
        path = Path.home() / ".claude.json"

    with open(path) as f:
        data = json.load(f)

    servers = data.get("mcpServers", {})
    registry: dict[str, ServiceEntry] = {}

    for raw_name, cfg in servers.items():
        name = normalize_name(raw_name)
        if not name:
            continue

        transport_type = cfg.get("type", "stdio")

        guard = bool(cfg.get("guard", False))

        if transport_type == "http":
            entry = ServiceEntry(
                name=name,
                original_name=raw_name,
                transport_type="http",
                url=cfg["url"],
                guard=guard,
            )
        else:
            entry = ServiceEntry(
                name=name,
                original_name=raw_name,
                transport_type="stdio",
                command=cfg.get("command"),
                args=cfg.get("args", []),
                env=cfg.get("env", {}),
                guard=guard,
            )

        registry[name] = entry

    return registry


WEBSPEC_DIR = Path.home() / ".webspec"
SESSION_KEY_PATH = WEBSPEC_DIR / "session.key"


def get_session_key() -> bytes:
    """Load or generate the 32-byte session key for bookend HMACs."""
    WEBSPEC_DIR.mkdir(parents=True, exist_ok=True)

    if SESSION_KEY_PATH.exists():
        key = SESSION_KEY_PATH.read_bytes()
        if len(key) == 32:
            return key

    key = secrets.token_bytes(32)
    SESSION_KEY_PATH.write_bytes(key)
    os.chmod(SESSION_KEY_PATH, 0o600)
    return key


class ServiceRegistry:
    """Live registry with config reload support."""

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or (Path.home() / ".claude.json")
        self._services: dict[str, ServiceEntry] = {}
        self._mtime: float = 0.0
        self.reload()

    def reload(self) -> None:
        try:
            stat = self._config_path.stat()
            self._mtime = stat.st_mtime
        except OSError:
            return
        self._services = parse_claude_config(self._config_path)

    def check_reload(self) -> bool:
        """Check if config file changed, reload if so. Returns True if reloaded."""
        try:
            stat = self._config_path.stat()
        except OSError:
            return False
        if stat.st_mtime != self._mtime:
            old_services = set(self._services.keys())
            self.reload()
            return old_services != set(self._services.keys()) or True
        return False

    @property
    def services(self) -> dict[str, ServiceEntry]:
        return self._services

    def get(self, name: str) -> ServiceEntry | None:
        return self._services.get(name)

    def names(self) -> list[str]:
        return list(self._services.keys())
