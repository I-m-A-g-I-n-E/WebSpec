from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

import uvicorn

from .app import create_registry_app
from .catalog import harvest, http_fetch_tools

logger = logging.getLogger("webspec.registry")

# TTL (seconds) for the cached default catalog — see _cached_default_catalog.
CATALOG_TTL = 60

# TODO(C): _cached_default_catalog has no lock (concurrent cache-miss requests
# can each re-harvest — thundering herd), and _default_catalog does blocking
# urllib inside async endpoints (blocks the event loop). Move harvest
# off-loop / add a single-flight lock. See docs/ROADMAP-C.md.

# Module-level cache state for _cached_default_catalog. Deliberately simple
# (stdlib only, single-process): a (value, timestamp) pair guarded by TTL.
_cache_value = None
_cache_time = None


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


def _get_guard_key() -> bytes | None:
    """Obtain the gateway's guard session key, if configured.

    get_session_key() reads WEBSPEC_GUARD_KEY and fails closed (raises) if
    absent. Catch broadly and default to None so the registry still runs
    unauthenticated (as before) when no key is configured or webspec isn't
    importable — guard-aware harvest is strictly additive.
    """
    try:
        from webspec.config import get_session_key
        return get_session_key()
    except Exception as e:
        # Not an error — unguarded deployments legitimately have no key. Log at
        # debug so a genuine misconfig is still diagnosable without spamming the
        # 60s refresh. The exception text never contains key bytes.
        logger.debug("guard key unavailable, harvesting unauthenticated: %s", e)
        return None


def _default_catalog():
    gateway_url = os.environ.get("WEBSPEC_GATEWAY_URL", "http://localhost:7002")
    services = _discover_services(gateway_url)
    guard_key = _get_guard_key()
    return harvest(services, http_fetch_tools(gateway_url, guard_key=guard_key))


def _cached_default_catalog():
    """TTL-cached wrapper around _default_catalog.

    /resolve, /catalog, and /graph each call the catalog function on every
    request, and the underlying harvest does blocking discovery + one HTTP
    round-trip per service. Cache the result for CATALOG_TTL seconds so a
    burst of requests reuses one harvest instead of re-harvesting per
    request. A down gateway still degrades to [] (see _discover_services);
    that empty result may itself be cached for the TTL, which is fine.
    """
    global _cache_value, _cache_time
    now = time.monotonic()
    if _cache_value is not None and _cache_time is not None and (now - _cache_time) < CATALOG_TTL:
        return _cache_value
    _cache_value = _default_catalog()
    _cache_time = now
    return _cache_value


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
    app = create_registry_app(catalog_fn=_cached_default_catalog)
    # localhost-only for tier B (see create_registry_app note).
    uvicorn.run(app, host=_resolve_registry_host(), port=port)


if __name__ == "__main__":
    main()
