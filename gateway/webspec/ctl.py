"""webspec-ctl: provision and manage WebSpec services.

Usage:
    webspec-ctl add <name> [options]
    webspec-ctl ls
    webspec-ctl health [name]
    webspec-ctl rm <name> [--clean-env]
    webspec-ctl caddy-sync
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from .caddy import (
    generate_direct_site_block,
    generate_site_block,
    reload_caddy,
    remove_site_block,
    sync_caddy_config,
    write_site_block,
)
from .config import ServiceRegistry, normalize_name
from .config_writer import (
    add_env_var,
    add_service,
    list_services,
    remove_env_var,
    remove_service,
)

CADDY_PORT = int(os.environ.get("WEBSPEC_PORT", "7001"))
GATEWAY_PORT = int(os.environ.get("WEBSPEC_INTERNAL_PORT", "7002"))
DOMAIN = os.environ.get("WEBSPEC_DOMAIN", "i-a-m.live")


def _health_check(name: str, port: int = CADDY_PORT, timeout: float = 5.0) -> bool:
    """HEAD request to service subdomain. Returns True if reachable."""
    url = f"http://{name}.localhost:{port}/"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def _wait_for_gateway(name: str, port: int = GATEWAY_PORT, max_wait: float = 35.0) -> bool:
    """Poll gateway until service appears (config reload cycle)."""
    url = f"http://{name}.localhost:{port}/"
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        req = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=3):
                return True
        except urllib.error.HTTPError as e:
            # 401/403 means service exists but requires auth — that's fine
            if e.code in (401, 403):
                return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(2)
    return False


def _public_url(name: str, https: bool = False) -> str:
    """Format the public URL for display."""
    scheme = "https" if https else "http"
    return f"{scheme}://{name}.{DOMAIN}"


def cmd_add(args: argparse.Namespace) -> int:
    """Provision a new service."""
    raw_name = args.name
    name = normalize_name(raw_name)
    if not name:
        print(f"Error: invalid service name '{raw_name}'", file=sys.stderr)
        return 1

    # Check for duplicates
    existing = list_services()
    if name in existing and not args.force:
        print(f"Error: service '{name}' already exists (use --force to update)", file=sys.stderr)
        return 1

    # --port shorthand: expand to --type http --url http://localhost:PORT --no-guard
    if args.port:
        args.svc_type = "http"
        args.url = args.url or f"http://localhost:{args.port}"
        args.guard = False

    # --direct: Caddy-only proxy, bypass gateway entirely
    if args.direct:
        if not args.port and not args.url:
            print("Error: --direct requires --port or --url", file=sys.stderr)
            return 1
        target_port = args.port or int(args.url.rsplit(":", 1)[-1].rstrip("/"))

        content = generate_direct_site_block(
            name=name, domain=DOMAIN,
            target_port=target_port, caddy_port=CADDY_PORT,
        )
        write_site_block(name, content)
        print(f"Generated direct Caddy proxy for {name} → localhost:{target_port}")

        if reload_caddy():
            print("Caddy reloaded")
        else:
            print("Warning: Caddy reload failed (is Caddy running?)", file=sys.stderr)

        # Quick health check
        healthy = _health_check(name, port=CADDY_PORT)
        status = "ok" if healthy else "unreachable"

        print()
        print(f"  Name:    {name}")
        print(f"  Type:    direct")
        print(f"  Guard:   no")
        print(f"  Health:  {status}")
        print(f"  URL:     {_public_url(name, args.https)}")
        return 0

    # Build service config
    entry: dict = {}

    if args.svc_type == "http":
        if not args.url:
            print("Error: --url required for http services", file=sys.stderr)
            return 1
        entry["type"] = "http"
        entry["url"] = args.url
        if args.header:
            headers = {}
            for h in args.header:
                if ":" not in h:
                    print(f"Error: invalid header format '{h}' (expected KEY:VALUE)", file=sys.stderr)
                    return 1
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()
            entry["headers"] = headers
    else:
        if not args.svc_command:
            print("Error: --command required for stdio services", file=sys.stderr)
            return 1
        entry["type"] = "stdio"
        entry["command"] = args.svc_command
        if args.svc_args:
            entry["args"] = args.svc_args
        if args.svc_env:
            env = {}
            for e in args.svc_env:
                if "=" not in e:
                    print(f"Error: invalid env format '{e}' (expected KEY=VALUE)", file=sys.stderr)
                    return 1
                k, v = e.split("=", 1)
                env[k] = v
            entry["env"] = env

    if args.guard:
        entry["guard"] = True

    if args.namespace:
        entry["namespace"] = args.namespace

    # Write config
    add_service(name, entry)
    print(f"Added service '{name}' to ~/.claude.json")

    # Add secret placeholders
    if args.secret:
        for s in args.secret:
            add_env_var(s)
            print(f"Added placeholder {s} to ~/.env")

    # Generate Caddy site block
    content = generate_site_block(
        name=name, domain=DOMAIN,
        gateway_port=GATEWAY_PORT, guard=args.guard,
        caddy_port=CADDY_PORT,
    )
    write_site_block(name, content)
    print(f"Generated Caddy config for {name}")

    # Reload Caddy
    if reload_caddy():
        print("Caddy reloaded")
    else:
        print("Warning: Caddy reload failed (is Caddy running?)", file=sys.stderr)

    # Wait for gateway config reload
    print("Waiting for gateway to pick up config...", end=" ", flush=True)
    if _wait_for_gateway(name, port=GATEWAY_PORT):
        print("ready")
    else:
        print("timeout (gateway may need manual restart)", file=sys.stderr)

    # Health check through Caddy
    healthy = _health_check(name, port=CADDY_PORT)
    status = "ok" if healthy else "unreachable"

    print()
    print(f"  Name:    {name}")
    print(f"  Type:    {args.svc_type}")
    print(f"  Guard:   {'yes' if args.guard else 'no'}")
    print(f"  Health:  {status}")
    print(f"  URL:     {_public_url(name, args.https)}")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    """List services with status."""
    services = list_services()
    if not services:
        print("No services configured.")
        return 0

    # Header
    print(f"{'NAME':<20} {'TYPE':<8} {'GUARD':<8} {'HEALTH':<10} {'URL'}")
    print("-" * 75)

    for name, cfg in sorted(services.items()):
        normalized = normalize_name(name)
        svc_type = cfg.get("type", "stdio")
        guard = "yes" if cfg.get("guard") else "no"
        healthy = _health_check(normalized, port=CADDY_PORT, timeout=2.0)
        health = "ok" if healthy else "down"
        url = f"{normalized}.{DOMAIN}"
        print(f"{normalized:<20} {svc_type:<8} {guard:<8} {health:<10} {url}")

    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Detailed health check for a service (or all)."""
    services = list_services()
    targets = [args.name] if args.name else list(services.keys())

    for raw_name in targets:
        name = normalize_name(raw_name)
        print(f"\n--- {name} ---")

        # Caddy proxy check
        caddy_ok = _health_check(name, port=CADDY_PORT, timeout=3.0)
        print(f"  Caddy proxy:  {'reachable' if caddy_ok else 'unreachable'}")

        # Gateway direct check
        gateway_ok = _health_check(name, port=GATEWAY_PORT, timeout=3.0)
        print(f"  Gateway:      {'reachable' if gateway_ok else 'unreachable'}")

        # Registry check
        try:
            registry = ServiceRegistry()
            entry = registry.get(name)
            print(f"  Registry:     {'found' if entry else 'not found'}")
            if entry:
                print(f"  Guard:        {'yes' if entry.guard else 'no'}")
        except Exception:
            print("  Registry:     error reading config")

    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    """Deprovision a service."""
    name = normalize_name(args.name)
    if not name:
        print(f"Error: invalid service name '{args.name}'", file=sys.stderr)
        return 1

    # Remove from ~/.claude.json
    remove_service(name)
    print(f"Removed '{name}' from ~/.claude.json")

    # Remove Caddy site block
    remove_site_block(name)
    print(f"Removed Caddy config for {name}")

    # Reload Caddy
    if reload_caddy():
        print("Caddy reloaded")
    else:
        print("Warning: Caddy reload failed", file=sys.stderr)

    # Clean env vars if requested
    if args.clean_env:
        for key in args.clean_env:
            remove_env_var(key)
            print(f"Removed {key} from ~/.env")

    print(f"Service '{name}' deprovisioned. Gateway will drop it on next reload cycle.")
    return 0


def cmd_caddy_sync(args: argparse.Namespace) -> int:
    """Regenerate all Caddy configs from current registry."""
    registry = ServiceRegistry()
    added, removed = sync_caddy_config(
        registry, domain=DOMAIN,
        gateway_port=GATEWAY_PORT, caddy_port=CADDY_PORT,
    )

    if added:
        print(f"Added:   {', '.join(added)}")
    if removed:
        print(f"Removed: {', '.join(removed)}")
    if not added and not removed:
        print("All Caddy configs up to date.")

    if reload_caddy():
        print("Caddy reloaded")
    else:
        print("Warning: Caddy reload failed", file=sys.stderr)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webspec-ctl",
        description="Provision and manage WebSpec services",
    )
    sub = parser.add_subparsers(dest="subcmd", required=True)

    # add
    add_p = sub.add_parser("add", help="Provision a new service")
    add_p.add_argument("name", help="Service name (will be normalized to subdomain)")
    add_p.add_argument("--type", choices=["http", "stdio"], default="http", dest="svc_type")
    add_p.add_argument("-p", "--port", type=int,
                       help="Shorthand: proxy to localhost:PORT (implies --type http --no-guard)")
    add_p.add_argument("--url", help="Remote MCP server URL (required for http)")
    add_p.add_argument("--header", action="append", metavar="KEY:VALUE",
                       help="HTTP header (repeatable, supports ${ENV_VAR})")
    add_p.add_argument("--command", dest="svc_command",
                       help="Command to run (required for stdio)")
    add_p.add_argument("--args", action="append", metavar="ARG", dest="svc_args",
                       help="Command arguments (repeatable)")
    add_p.add_argument("--env", action="append", metavar="KEY=VALUE", dest="svc_env",
                       help="Environment variable (repeatable)")
    add_p.add_argument("--guard", action="store_true", default=True,
                       help="Require HMAC auth (default)")
    add_p.add_argument("--no-guard", dest="guard", action="store_false",
                       help="Disable HMAC auth")
    add_p.add_argument("--secret", action="append", metavar="ENV_VAR",
                       help="Add placeholder to ~/.env for this var")
    add_p.add_argument("--direct", action="store_true",
                       help="Direct Caddy proxy (bypass gateway, for non-MCP web apps)")
    add_p.add_argument("--https", action="store_true",
                       help="Display public URL with https:// (TLS via Cloudflare)")
    add_p.add_argument("--namespace", help="Namespace scheme (phase 2)")
    add_p.add_argument("--force", action="store_true",
                       help="Overwrite existing service")

    # ls
    sub.add_parser("ls", help="List services with status")

    # health
    health_p = sub.add_parser("health", help="Detailed health check")
    health_p.add_argument("name", nargs="?", help="Service name (all if omitted)")

    # rm
    rm_p = sub.add_parser("rm", help="Deprovision a service")
    rm_p.add_argument("name", help="Service name to remove")
    rm_p.add_argument("--clean-env", nargs="*", metavar="KEY",
                      help="Remove env vars from ~/.env")

    # caddy-sync
    sub.add_parser("caddy-sync", help="Regenerate all Caddy configs from registry")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "add": cmd_add,
        "ls": cmd_ls,
        "health": cmd_health,
        "rm": cmd_rm,
        "caddy-sync": cmd_caddy_sync,
    }

    return commands[args.subcmd](args)


if __name__ == "__main__":
    sys.exit(main())
