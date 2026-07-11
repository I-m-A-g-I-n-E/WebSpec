from __future__ import annotations

import json
import logging
import urllib.request

from .normalize import normalize, tier_of
from .records import ToolRecord

logger = logging.getLogger("webspec.registry.catalog")


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


def http_fetch_tools(gateway_url: str):
    """Return a fetch_tools callable that reads OPTIONS {gateway_url}/ per service subdomain.

    The gateway's index (GET {gateway_url}/) lists services; OPTIONS {service}.<host>/ lists tools.
    Here we hit the gateway index for the tool list per service via its JSON `tools` array.
    """
    def _fetch(service: str) -> list[dict]:
        # The gateway serves OPTIONS /{service}/ as {"service","tools":[...]}
        url = gateway_url.rstrip("/") + "/"
        # TODO(C): guard-aware harvesting — guarded services return 401 to unauthenticated
        # OPTIONS and are skipped by harvest()'s except (see catalog.harvest); harvesting them
        # requires sending the guard HMAC + nonce bootstrap. See docs/ROADMAP-C.md.
        req = urllib.request.Request(url, method="OPTIONS", headers={"Host": f"{service}.localhost"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read())
        return payload.get("tools", [])
    return _fetch
