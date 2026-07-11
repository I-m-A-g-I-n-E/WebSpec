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
    and returns an empty list so the registry degrades gracefully.
    """
    try:
        url = gateway_url.rstrip("/") + "/"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return [s["name"] for s in data.get("services", [])]
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as e:
        logger.warning("registry: gateway discovery failed, degrading to empty catalog (%s)", e)
        return []


def _default_catalog():
    gateway_url = os.environ.get("WEBSPEC_GATEWAY_URL", "http://localhost:7002")
    services = _discover_services(gateway_url)
    return harvest(services, http_fetch_tools(gateway_url))


def main() -> None:
    port = int(os.environ.get("WEBSPEC_INTERNAL_PORT", "7003"))
    app = create_registry_app(catalog_fn=_default_catalog)
    # localhost-only for tier B (see create_registry_app note).
    uvicorn.run(app, host=os.environ.get("WEBSPEC_HOST", "127.0.0.1"), port=port)


if __name__ == "__main__":
    main()
