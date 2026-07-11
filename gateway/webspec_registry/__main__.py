from __future__ import annotations

import os

import uvicorn

from .app import create_registry_app
from .catalog import harvest, http_fetch_tools


def _default_catalog():
    gateway_url = os.environ.get("WEBSPEC_GATEWAY_URL", "http://localhost:7002")
    # Discover service names from the gateway index, then harvest each.
    import json
    import urllib.request
    with urllib.request.urlopen(gateway_url.rstrip("/") + "/", timeout=5) as resp:
        services = [s["name"] for s in json.loads(resp.read()).get("services", [])]
    return harvest(services, http_fetch_tools(gateway_url))


def main() -> None:
    port = int(os.environ.get("WEBSPEC_INTERNAL_PORT", "7003"))
    app = create_registry_app(catalog_fn=_default_catalog)
    # localhost-only for tier B (see create_registry_app note).
    uvicorn.run(app, host=os.environ.get("WEBSPEC_HOST", "127.0.0.1"), port=port)


if __name__ == "__main__":
    main()
