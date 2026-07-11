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


def _options_request(url: str, host: str, extra_headers: dict | None = None) -> list[dict]:
    headers = {"Host": host}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, method="OPTIONS", headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = json.loads(resp.read())
    return payload.get("tools", [])


def _bootstrap_nonce(gateway_url: str, host: str, guard_key: bytes) -> str:
    """GET {gateway_url}/__nonce with the bootstrap HMAC (nonce field empty, body empty)."""
    nonce_url = gateway_url.rstrip("/") + "/__nonce"
    mac = compute_guard_hmac(guard_key, "GET", host, "/__nonce", "", b"")
    req = urllib.request.Request(nonce_url, method="GET", headers={"Host": host, "X-WebSpec-Guard": mac})
    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = json.loads(resp.read())
    return payload["nonce"]


def _guarded_options(url: str, host: str, guard_key: bytes, nonce: str) -> list[dict]:
    mac = compute_guard_hmac(guard_key, "OPTIONS", host, "/", nonce, b"")
    return _options_request(url, host, extra_headers={"X-WebSpec-Guard": mac, "X-WebSpec-Nonce": nonce})


def http_fetch_tools(gateway_url: str, guard_key: bytes | None = None):
    """Return a fetch_tools callable that reads OPTIONS {gateway_url}/ per service subdomain.

    The gateway's index (GET {gateway_url}/) lists services; OPTIONS {service}.<host>/ lists tools.
    Here we hit the gateway index for the tool list per service via its JSON `tools` array.

    Guard-aware: if the unauthenticated OPTIONS is rejected with 401/403 and a
    guard_key is supplied (and webspec.guard is importable), retry via the
    gateway's own guard flow — GET /__nonce (bootstrap HMAC) then OPTIONS /
    with the guard HMAC + nonce. Unguarded services are unaffected: they
    succeed on the first unauthenticated OPTIONS and never take this path.
    If the guarded retry also fails, the HTTPError propagates so harvest()
    skips the service exactly as it does today — this never crashes.
    """
    def _fetch(service: str) -> list[dict]:
        # The gateway serves OPTIONS /{service}/ as {"service","tools":[...]}
        url = gateway_url.rstrip("/") + "/"
        host = f"{service}.localhost"
        try:
            return _options_request(url, host)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and guard_key is not None and compute_guard_hmac is not None:
                nonce = _bootstrap_nonce(gateway_url, host, guard_key)
                return _guarded_options(url, host, guard_key, nonce)
            raise
    return _fetch
