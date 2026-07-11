from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

import uvicorn

from .app import create_registry_app
from .catalog import harvest, http_fetch_tools

logger = logging.getLogger("webspec.registry")


def _discover_services(gateway_url: str) -> list[str]:
    """Discover service names from the gateway index.

    On any connection error, JSON parse error, or value error, logs a warning
    and returns an empty list so the registry degrades gracefully. Entries in
    the "services" array that are malformed (not a dict, or missing "name")
    are skipped rather than raising.
    """
    try:
        url = gateway_url.rstrip("/") + "/"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return [s["name"] for s in data.get("services", []) if isinstance(s, dict) and s.get("name")]
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
        logger.warning("registry: gateway discovery failed, degrading to empty catalog (%s)", e)
        return []


def _default_catalog():
    gateway_url = os.environ.get("WEBSPEC_GATEWAY_URL", "http://localhost:7002")
    services = _discover_services(gateway_url)
    return harvest(services, http_fetch_tools(gateway_url))


def _resolve_registry_host() -> str:
    """Resolve the bind host for the registry service.

    Deliberately reads WEBSPEC_REGISTRY_HOST, NOT the gateway's WEBSPEC_HOST.
    The gateway sets WEBSPEC_HOST=0.0.0.0 (see docker-compose); if the
    registry inherited that shared env var it would silently bind all
    interfaces and expose the tool inventory. The registry is hard-localhost
    for tier B, so it needs its own, distinct opt-in knob.
    """
    return os.environ.get("WEBSPEC_REGISTRY_HOST", "127.0.0.1")


def main() -> None:
    port = int(os.environ.get("WEBSPEC_INTERNAL_PORT", "7003"))
    app = create_registry_app(catalog_fn=_default_catalog)
    # localhost-only for tier B (see create_registry_app note).
    uvicorn.run(app, host=_resolve_registry_host(), port=port)


if __name__ == "__main__":
    main()
