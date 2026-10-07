from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

import uvicorn

from .app import create_registry_app
from .catalog import harvest, http_fetch_tools, open_gateway

logger = logging.getLogger("webspec.registry")

# TTL (seconds) for the cached default catalog — see _cached_default_catalog.
CATALOG_TTL = 60

# Where the gateway listens (WEBSPEC_GATEWAY_URL overrides it). By IP: see _gateway_url.
DEFAULT_GATEWAY_URL = "http://127.0.0.1:7002"

# Where the registry listens (WEBSPEC_REGISTRY_PORT overrides it). See _resolve_registry_port.
DEFAULT_REGISTRY_PORT = 7004

# Guarded destinations are harvested only when this is "1": it needs the guard key, and the
# registry then serves their tool lists without the guard (see _protect_guard_key).
HARVEST_GUARDED_ENV = "WEBSPEC_REGISTRY_HARVEST_GUARDED"

# A destination name from the index becomes a Host label ({name}.localhost), so it must be one:
# the gateway's own rule (webspec.hostgrammar, HG-2), kept here for a registry without webspec.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

# TODO(C): _cached_default_catalog has no lock (concurrent cache-miss requests
# can each re-harvest — thundering herd), and _default_catalog does blocking
# urllib inside async endpoints (blocks the event loop). Move harvest
# off-loop / add a single-flight lock. See docs/spec/status.md (Roadmap).

# Module-level cache state for _cached_default_catalog. Deliberately simple
# (stdlib only, single-process): a (value, timestamp) pair guarded by TTL.
_cache_value = None
_cache_time = None


def _resolves_to_loopback(hostname: str, port: int | None) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, port or 80, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False  # unresolvable: the connection fails anyway
    return any(ipaddress.ip_address(str(info[4][0]).split("%")[0]).is_loopback for info in infos)


def _gateway_url() -> str:
    """WEBSPEC_GATEWAY_URL, or DEFAULT_GATEWAY_URL: where the registry connects to the gateway.

    A gateway on this host is dialed by IP, as webspec.caddy dials it. A name such as
    ``localhost`` may resolve to ::1 first while the gateway listens on 127.0.0.1 only, and any
    local user can listen on [::1] at that port and receive the registry's requests (and, with
    guarded harvest on, its guard tags, for destinations of its choosing). So a URL whose host
    is a name for a loopback address is refused with ValueError. The Host header is set
    explicitly on every request, so the URL only says where to connect.
    """
    url = os.environ.get("WEBSPEC_GATEWAY_URL", "").strip() or DEFAULT_GATEWAY_URL
    parts = urllib.parse.urlsplit(url)
    try:
        hostname, port = (parts.hostname or "").rstrip("."), parts.port
    except ValueError:  # a port that is not a number
        hostname, port = "", None
    if parts.scheme not in ("http", "https") or not hostname:
        raise ValueError(f"WEBSPEC_GATEWAY_URL {url!r} is not an http:// URL")
    try:
        ipaddress.ip_address(hostname)
        return url
    except ValueError:
        pass
    if hostname == "localhost" or hostname.endswith(".localhost") or _resolves_to_loopback(hostname, port):
        raise ValueError(f"WEBSPEC_GATEWAY_URL {url!r} names a loopback address; dial the gateway by IP, "
                         f"as in {DEFAULT_GATEWAY_URL}, so no other local user can take the registry's requests")
    return url


def _discover_services(gateway_url: str) -> list[str]:
    """Discover service names from the gateway index.

    On any connection error, JSON parse error, or value error, logs a warning
    and returns an empty list so the registry degrades gracefully. Entries in
    the "services" array that are malformed (not a dict, or a "name" that is
    not a lowercase DNS label) are skipped rather than raising.
    """
    try:
        url = gateway_url.rstrip("/") + "/"
        # The gateway routes by Host: its index answers on the loopback name, whatever the address.
        req = urllib.request.Request(url, headers={"Host": "localhost"})
        with open_gateway(req, timeout=5) as resp:
            data = json.loads(resp.read())
            names = [s.get("name") for s in data.get("services", []) if isinstance(s, dict)]
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError, KeyError, TypeError,
            AttributeError) as e:
        logger.warning("registry: gateway discovery failed, degrading to empty catalog (%s)", e)
        return []
    return [n for n in names if isinstance(n, str) and _LABEL.fullmatch(n)]


def _harvest_guarded() -> bool:
    return os.environ.get(HARVEST_GUARDED_ENV) == "1"


def _get_guard_key() -> bytes | None:
    """The gateway's guard key, only when WEBSPEC_REGISTRY_HARVEST_GUARDED=1. Off by default.

    DP-1: the key must stay out of every process of the agent's user, and the registry is
    typically started from a login shell. Without the key it inventories unguarded
    destinations only. Turn guarded harvest on only for a registry that runs as the gateway's
    own user, which can read the key, and only where every local process may see the guarded
    destinations' tool lists, which the registry then serves without the guard (GD-1); main()
    makes the process non-dumpable (DP-2).
    """
    if not _harvest_guarded():
        return None
    try:
        from webspec.config import get_session_key
        return get_session_key()
    except Exception as e:
        # The operator asked for guarded harvest, so say why it is off. The exception text
        # never contains key bytes (GuardKeyError names the variable or the file).
        logger.warning("registry: %s=1, but the guard key is unavailable, so guarded destinations are "
                       "skipped: %s", HARVEST_GUARDED_ENV, e)
        return None


def _default_catalog():
    try:
        gateway_url = _gateway_url()
    except ValueError as e:
        logger.warning("registry: %s; degrading to empty catalog", e)
        return []
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

    An empty (or blank) value counts as unset, as for the registry's port and the gateway's
    WEBSPEC_HOST: uvicorn would bind "" on every interface, and an empty value is what
    compose's ``${VAR}`` gives for an unset variable, or an env-file line ``NAME=``.
    """
    return (os.environ.get("WEBSPEC_REGISTRY_HOST") or "").strip() or "127.0.0.1"


def _resolve_registry_port() -> int:
    """The registry's listen port: WEBSPEC_REGISTRY_PORT, default 7004. ValueError if malformed.

    Deliberately not WEBSPEC_INTERNAL_PORT, the gateway's listen port (7002), for the same
    reason as _resolve_registry_host: a registry started with the gateway's environment would
    try to take the gateway's port. 7001 and 7003 are taken as well: on Linux, Caddy's socket
    holds them on loopback, 7001 for the gateway and 7003 for direct sites, and cloudflared
    sends public traffic to both. A registry that got one of them while that socket was down
    would serve the tool inventory to the internet.
    """
    raw = os.environ.get("WEBSPEC_REGISTRY_PORT", "").strip()
    if not raw:
        return DEFAULT_REGISTRY_PORT
    port = int(raw) if raw.isascii() and raw.isdigit() else 0
    if not 1 <= port <= 65535:
        raise ValueError(f"WEBSPEC_REGISTRY_PORT {raw!r} is not a port number")
    return port


def _protect_guard_key(host: str, port: int) -> None:
    """DP-1, DP-2: hold the guard key only when guarded harvest asks for it, and then harden.

    Guarded harvest also republishes what GD-1 keeps behind the guard: the tool lists of
    guarded destinations (names, descriptions, input schemas), which /catalog, /resolve and
    /graph serve, unauthenticated, to every process that can reach the registry, the
    agent's included. The warning says so, with the address.
    """
    if _harvest_guarded():
        try:
            from webspec.hardening import harden_process
        except ImportError:  # pragma: no cover - webspec is installed alongside
            harden_process = None
        if harden_process is not None:
            harden_process()  # non-dumpable on Linux, like the gateway (DP-2)
        logger.warning("registry: %s=1: this process reads the guard key, so run it as the gateway's own "
                       "user, never as the agent's (DP-1). It also serves the tool lists of guarded "
                       "destinations (names, descriptions, input schemas), which the gateway gives only to "
                       "guarded requests (GD-1), without the guard to every process that can reach "
                       "%s:%d, the agent's included.", HARVEST_GUARDED_ENV,
                       f"[{host}]" if ":" in host else host, port)
        return
    named = [v for v in ("WEBSPEC_GUARD_KEY", "WEBSPEC_GUARD_KEY_FILE") if os.environ.get(v, "").strip()]
    if named:
        logger.warning("registry: %s is set but not used: guarded harvest is off (%s). Do not give the "
                       "guard key to a process of the agent's user (DP-1).", " and ".join(named),
                       HARVEST_GUARDED_ENV)


def main() -> None:
    try:
        _gateway_url()
        port = _resolve_registry_port()
    except ValueError as e:
        raise SystemExit(f"webspec-registry: {e}") from None
    if os.environ.get("WEBSPEC_INTERNAL_PORT", "").strip() and not os.environ.get("WEBSPEC_REGISTRY_PORT"):
        logger.warning("registry: WEBSPEC_INTERNAL_PORT is the gateway's listen port and is ignored; the "
                       "registry listens on WEBSPEC_REGISTRY_PORT (default %d)", DEFAULT_REGISTRY_PORT)
    host = _resolve_registry_host()
    _protect_guard_key(host, port)
    app = create_registry_app(catalog_fn=_cached_default_catalog)
    # localhost-only for tier B (see create_registry_app note).
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
