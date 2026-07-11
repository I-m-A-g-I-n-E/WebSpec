"""Caddy config generation and reload for per-service site blocks."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ServiceRegistry

logger = logging.getLogger("webspec.caddy")

CADDY_CONF_DIR = Path("/etc/caddy/conf.d")
CADDY_LOG_DIR = Path("/var/log/caddy")
DEFAULT_RATE_LIMIT = 60  # requests per minute per IP
DEFAULT_RATE_WINDOW = "1m"


def generate_direct_site_block(
    name: str,
    domain: str,
    target_port: int,
    caddy_port: int = 7001,
) -> str:
    """Generate a Caddy site block for a direct (non-MCP) web app.

    Proxies directly to the target port, bypassing the gateway entirely.
    No rate limiting on MCP endpoints (there are none).
    """
    hosts = f"http://{name}.localhost:{caddy_port}, http://{name}.{domain}:{caddy_port}"

    lines = [
        f"{hosts} {{",
        f"    reverse_proxy localhost:{target_port}",
        f"    log {{",
        f"        output file {CADDY_LOG_DIR}/{name}.log {{",
        f"            roll_size 10mb",
        f"            roll_keep 5",
        f"        }}",
        f"        format json",
        f"    }}",
        "}",
    ]
    return "\n".join(lines) + "\n"


def generate_site_block(
    name: str,
    domain: str,
    gateway_port: int = 7002,
    guard: bool = False,
    rate_limit: int = DEFAULT_RATE_LIMIT,
    rate_window: str = DEFAULT_RATE_WINDOW,
    caddy_port: int = 7001,
) -> str:
    """Generate a Caddy site block for a service.

    Returns the Caddy config text for one service subdomain.
    """
    hosts = f"http://{name}.localhost:{caddy_port}, http://{name}.{domain}:{caddy_port}"

    lines = [
        f"{hosts} {{",
        f"    reverse_proxy localhost:{gateway_port}",
        f"    log {{",
        f"        output file {CADDY_LOG_DIR}/{name}.log {{",
        f"            roll_size 10mb",
        f"            roll_keep 5",
        f"        }}",
        f"        format json",
        f"    }}",
    ]

    # Rate limiting (skip health/auth endpoints)
    lines.extend([
        f"    @notHealth not path /__nonce /__challenge",
        f"    rate_limit @notHealth {{",
        f"        zone {name} {{",
        f"            key    {{remote_host}}",
        f"            events {rate_limit}",
        f"            window {rate_window}",
        f"        }}",
        f"    }}",
    ])

    lines.append("}")
    return "\n".join(lines) + "\n"


def write_site_block(name: str, content: str, conf_dir: Path | None = None) -> Path:
    """Write a site block to the Caddy conf.d directory. Returns the file path."""
    d = conf_dir or CADDY_CONF_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.caddy"
    p.write_text(content)
    return p


def remove_site_block(name: str, conf_dir: Path | None = None) -> None:
    """Remove a service's Caddy config file."""
    d = conf_dir or CADDY_CONF_DIR
    p = d / f"{name}.caddy"
    if p.exists():
        p.unlink()
        logger.info("Removed Caddy config: %s", p)


def sync_caddy_config(
    registry: ServiceRegistry,
    domain: str,
    gateway_port: int = 7002,
    caddy_port: int = 7001,
    conf_dir: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Regenerate all site blocks from registry, return (added, removed) names.

    Adds missing site blocks, removes stale ones.
    """
    d = conf_dir or CADDY_CONF_DIR
    d.mkdir(parents=True, exist_ok=True)

    # Current .caddy files on disk
    existing = {p.stem for p in d.glob("*.caddy")}
    # Services in registry
    wanted = set(registry.names())

    added = []
    removed = []

    # Add/update blocks for all registered services
    for name in wanted:
        entry = registry.get(name)
        if entry is None:
            continue
        content = generate_site_block(
            name=name,
            domain=domain,
            gateway_port=gateway_port,
            guard=entry.guard,
            caddy_port=caddy_port,
        )
        write_site_block(name, content, conf_dir=d)
        if name not in existing:
            added.append(name)

    # Remove stale blocks
    for name in existing - wanted:
        remove_site_block(name, conf_dir=d)
        removed.append(name)

    return sorted(added), sorted(removed)


def reload_caddy() -> bool:
    """Reload Caddy configuration. Returns True on success."""
    try:
        result = subprocess.run(
            ["caddy", "reload", "--config", "/etc/caddy/Caddyfile"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("Caddy reloaded successfully")
            return True
        logger.error("Caddy reload failed: %s", result.stderr)
        return False
    except FileNotFoundError:
        logger.error("caddy binary not found")
        return False
    except subprocess.TimeoutExpired:
        logger.error("Caddy reload timed out")
        return False
