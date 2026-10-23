from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .normalize import normalize, tier_of
from .records import ToolRecord

logger = logging.getLogger("webspec.registry.catalog")

# Reuse the gateway's own HMAC — never reimplement it (single source of
# truth for the guard scheme). If webspec isn't importable (e.g. the
# registry is deployed standalone without the gateway package), guard-aware
# harvest is simply disabled and we fall back to unauthenticated-only.
try:
    from webspec.guard import compute_guard_hmac
except Exception:  # pragma: no cover - exercised only when webspec is absent
    compute_guard_hmac = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # the 3xx becomes an HTTPError


# Every request to the gateway goes straight to the address in the URL: no proxy from the
# environment (http_proxy would receive the Host headers and any guard tags) and no redirect
# (urllib would carry the same headers to wherever it points). The gateway never redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def open_gateway(req: urllib.request.Request, timeout: float = 5):
    """urlopen() for requests to the gateway, without proxies or redirects."""
    return _OPENER.open(req, timeout=timeout)


def harvest(services: list[str], fetch_tools) -> list[ToolRecord]:
    """Build ToolRecords for each service. fetch_tools(service) -> list[{name,description,inputSchema}]."""
    records: list[ToolRecord] = []
    for svc in services:
        try:
            tools = fetch_tools(svc)
        except Exception as e:  # a down service must not sink the whole catalog
            logger.warning("catalog: skipping %s (%s)", svc, e)
            continue
        for t in tools:
            name = t.get("name", "")
            desc = t.get("description") or ""
            verb, noun = normalize(name, desc)
            records.append(ToolRecord(
                service=svc, tool=name, description=desc,
                verb=verb, noun=noun, tier=tier_of(svc, name, verb),
                input_schema=t.get("inputSchema") or {},
            ))
    return records


def _discovery_request(url: str, method: str, host: str, extra_headers: dict | None = None) -> dict:
    headers = {"Host": host}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, method=method, headers=headers)
    with open_gateway(req, timeout=5) as resp:
        return json.loads(resp.read())


def _tools(listing: dict, route_map: dict) -> list[dict]:
    """The destination's tools as harvest() takes them, from v2 discovery.

    The names and descriptions come from GET / (DS-3), each tool's inputSchema from the
    OPTIONS / entries (DS-1). An entry whose key is closed names no tool, so it adds no schema.
    """
    schemas = {e["tool"]: e.get("inputSchema") or {} for e in route_map.get("entries", []) if "tool" in e}
    return [{"name": t.get("name", ""), "description": t.get("description") or "",
             "inputSchema": schemas.get(t.get("name"), {})} for t in listing.get("tools", [])]


def _bootstrap_nonce(gateway_url: str, host: str, guard_key: bytes) -> str:
    """GET {gateway_url}/__nonce with the bootstrap HMAC (nonce field empty, body empty)."""
    nonce_url = gateway_url.rstrip("/") + "/__nonce"
    mac = compute_guard_hmac(guard_key, "GET", host, "/__nonce", "", b"")
    req = urllib.request.Request(nonce_url, method="GET", headers={"Host": host, "WebSpec-Guard": mac})
    with open_gateway(req, timeout=5) as resp:
        payload = json.loads(resp.read())
    return payload["nonce"]


def http_fetch_tools(gateway_url: str, guard_key: bytes | None = None):
    """Return a fetch_tools callable that reads each destination's GET / and OPTIONS /.

    The gateway's index (GET {gateway_url}/) lists services. On ``{service}.localhost``, GET /
    lists the tools with their descriptions (DS-3) and OPTIONS / gives the route map, whose
    entries carry each tool's inputSchema (DS-1). ``gateway_url`` says only where to connect
    (dial a loopback gateway by IP, see __main__._gateway_url); the Host header is always the
    loopback name ``{service}.localhost``.

    Guard-aware: if an unauthenticated request is rejected with 401/403 and a
    guard_key is supplied (and webspec.guard is importable), it and every later
    request to that destination go through the gateway's own guard flow — GET
    /__nonce (bootstrap HMAC), then the request with the guard HMAC + nonce.
    Unguarded services are unaffected: they succeed unauthenticated and never
    take this path. If a guarded request also fails, the HTTPError propagates so
    harvest() skips the service exactly as it does today — this never crashes.
    """
    url = gateway_url.rstrip("/") + "/"

    def _fetch(service: str) -> list[dict]:
        host = f"{service}.localhost"
        guarded = False

        def discover(method: str) -> dict:
            nonlocal guarded
            if not guarded:
                try:
                    return _discovery_request(url, method, host)
                except urllib.error.HTTPError as e:
                    if e.code not in (401, 403) or guard_key is None or compute_guard_hmac is None:
                        raise
                    guarded = True
            nonce = _bootstrap_nonce(gateway_url, host, guard_key)
            mac = compute_guard_hmac(guard_key, method, host, "/", nonce, b"")
            return _discovery_request(url, method, host, {"WebSpec-Guard": mac, "WebSpec-Nonce": nonce})

        return _tools(discover("GET"), discover("OPTIONS"))
    return _fetch
