"""Service registry: parse ~/.claude.json mcpServers and manage session keys."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .hostgrammar import is_label
from .methods import parse_level

logger = logging.getLogger("webspec.config")


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
    # http auth headers (env vars resolved at parse time)
    headers: dict[str, str] = field(default_factory=dict)
    # guard: require HMAC + nonce authentication (always true when level >= 1)
    guard: bool = False
    # Phase 2: namespace scheme (e.g. "user", "project")
    namespace: str | None = None
    # Security level 0-4 (docs/http-methods/method-profiles.md). Raw config value in;
    # normalized to an int by __post_init__ (absent → 1 if guard else 0; invalid → 4).
    level: int | None = None
    # Operator per-tool contract overrides: {tool_name: {read_only, destructive, idempotent,
    # open_world, tier}}. Authoritative over the server's annotations (may loosen).
    tools: dict[str, dict] = field(default_factory=dict)
    # Allowed qualifier labels left of this destination (hostgrammar.py), in canonical order.
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # One source of truth: guard <=> level >= 1. Take the stricter of the two.
        level = parse_level(self.level, guard=self.guard, service=self.name)
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "guard", level >= 1)


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


def _resolve_env(value: str) -> str:
    """Resolve ${VAR} patterns in a string from environment variables.

    Unresolvable variables are left as-is (no crash).
    """
    return re.sub(r'\$\{([^}]+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), value)


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
        level = cfg.get("level")  # normalized once, in ServiceEntry.__post_init__
        # Kept verbatim: a malformed value fails closed in methods.effective_contract().
        tools = cfg.get("tools") or {}
        raw_labels = cfg.get("labels") or []
        if isinstance(raw_labels, list) and all(isinstance(x, str) and is_label(x) for x in raw_labels):
            labels = tuple(raw_labels)
        else:
            logger.error("Service %s: 'labels' must be a list of lowercase DNS labels — allowing no qualifiers", name)
            labels = ()

        if transport_type == "http":
            raw_headers = cfg.get("headers", {})
            resolved_headers = {k: _resolve_env(v) for k, v in raw_headers.items()}
            entry = ServiceEntry(
                name=name,
                original_name=raw_name,
                transport_type="http",
                url=cfg["url"],
                headers=resolved_headers,
                guard=guard,
                namespace=cfg.get("namespace"),
                level=level,
                tools=tools,
                labels=labels,
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
                level=level,
                tools=tools,
                labels=labels,
            )

        registry[name] = entry

    return registry


_dev_ephemeral_key: bytes | None = None


class GuardKeyError(RuntimeError):
    """Raised when no guard key is available and none may be safely generated."""


def _derive_guard_key(raw: str) -> bytes:
    """64-hex → raw 32 bytes; anything else → sha256(utf8) so any passphrase works."""
    raw = raw.strip()
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    return hashlib.sha256(raw.encode()).digest()


def get_session_key() -> bytes:
    """Return the 32-byte guard key, sourced from the password manager via env.

    Populate WEBSPEC_GUARD_KEY from your vault at launch, e.g.:
        export WEBSPEC_GUARD_KEY=$(op read "op://WebSpec/gateway-guard/key")
    Fails closed if absent (no silent random key). Dev-only escape hatch:
    WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1 mints an insecure in-memory key.
    """
    raw = os.environ.get("WEBSPEC_GUARD_KEY")
    if raw and raw.strip():
        return _derive_guard_key(raw)

    if os.environ.get("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL") == "1":
        global _dev_ephemeral_key
        if _dev_ephemeral_key is None:
            _dev_ephemeral_key = secrets.token_bytes(32)
            print(
                "WARNING: WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1 — using an insecure "
                "in-memory guard key (dev only).",
                file=sys.stderr,
            )
        return _dev_ephemeral_key

    raise GuardKeyError(
        "WEBSPEC_GUARD_KEY is not set. Source it from your password manager, e.g. "
        "`export WEBSPEC_GUARD_KEY=$(op read 'op://WebSpec/gateway-guard/key')`. "
        "For local dev only, set WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1."
    )


class ServiceRegistry:
    """Live registry with config reload support."""

    def __init__(self, config_path: Path | None = None):
        if config_path is None:
            config_path = Path(os.environ.get("WEBSPEC_CONFIG", str(Path.home() / ".claude.json")))
        self._config_path = config_path
        self._services: dict[str, ServiceEntry] = {}
        self._mtime: float = 0.0
        self.reload()

    def reload(self) -> None:
        try:
            stat = self._config_path.stat()
            self._mtime = stat.st_mtime
        except OSError:
            return
        try:
            self._services = parse_claude_config(self._config_path)
        except (OSError, ValueError) as exc:
            # OSError covers IsADirectoryError (e.g. an empty Docker bind-mount
            # directory where a file was expected); ValueError covers
            # json.JSONDecodeError (malformed config). At startup, degrade to an
            # empty registry instead of crashing create_app(). On a later reload,
            # keep the last good registry: ~/.claude.json is rewritten often, and a
            # half-written file must not tear every service down.
            logger.warning(
                "Failed to parse config at %s (%s: %s) — %s.",
                self._config_path, type(exc).__name__, exc,
                "keeping the last good service registry" if self._services else "using empty service registry",
            )

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
