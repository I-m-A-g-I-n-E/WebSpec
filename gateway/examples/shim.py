"""A reference harness shim for WebSpec (spec: docs/spec/levels.md, "The harness shim").

The model decides only the method, the path, the arguments and, on POST, the definer verb
that names the action (SH-1). The path is a noun that the route rule gives a tool, such as
``/note`` for ``read_note`` and ``delete_note`` (PA-4). The shim adds everything cryptographic —
nonce, guard HMAC, Idempotency-Key, clearance — as the destination's level requires. It learns
the route map, and what each entry requires, from ``OPTIONS /`` (SH-2, DS-1): the entry for the
method and the path, and on POST for the definer, names the tool that a clearance binds. It
holds the guard key; the model never sees it.

Standard library only, and written from the spec text rather than from the gateway's code,
so it doubles as an executable reading of the spec: if the gateway accepts its requests, the
two agree.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode

SAFE = {"GET", "HEAD", "OPTIONS"}


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_query(query: str) -> str:
    """GD-3: decode, sort by key, re-encode with RFC 3986 percent-encoding."""
    return urlencode(sorted(parse_qsl(query, keep_blank_values=True)), quote_via=quote, safe="-._~")


def tag(key: bytes, message: bytes) -> str:
    """First 4 bytes of HMAC-SHA256, hex."""
    return hmac.new(key, message, hashlib.sha256).digest()[:4].hex()


def guard_tag(key: bytes, method: str, host: str, path: str, nonce: str, body: bytes,
              query: str = "", definer: str = "", idempotency_key: str = "") -> str:
    """GD-2."""
    message = f"{method}:{host}:{path}:{nonce}:{hashlib.sha256(body).hexdigest()}"
    if query:
        message += f":?{canonical_query(query)}"
    if definer:
        message += f":!{definer}"
    if idempotency_key:
        message += f":#{idempotency_key}"
    return tag(key, message.encode())


def clearance(key: bytes, destination: str, method: str, tool: str, args: dict, ts: int | None = None) -> str:
    """CL-1: ``tag:ts``, bound to destination, method, tool and arguments."""
    ts_text = str(int(time.time()) if ts is None else ts)
    message = f"ufo2:{destination}:{method}:{tool}:{canonical_json(args)}:{ts_text}"
    return f"{tag(key, message.encode())}:{ts_text}"


@dataclass
class Exchange:
    """One request/response pair, kept for transcripts."""

    method: str
    target: str
    headers: dict[str, str]
    body: bytes
    status: int
    response_headers: dict[str, str]
    response_body: bytes


@dataclass
class Shim:
    """Signs requests for one destination.

    ``http`` is any httpx-compatible client (``httpx.Client`` or Starlette's ``TestClient``).
    ``host`` is the Host header the gateway will see. ``vouch(method, tool, args)`` is the
    harness's clearance policy: return False to refuse to vouch (for example, when the
    arguments came from open-world output).
    """

    http: Any
    host: str
    destination: str
    key: bytes
    vouch: Callable[[str, str, dict], bool] = lambda method, tool, args: True
    log: list[Exchange] = field(default_factory=list)
    _map: dict | None = None

    def _send(self, method: str, path: str, query: str = "", body: bytes = b"",
              headers: dict[str, str] | None = None) -> Any:
        headers = {"Host": self.host, **(headers or {})}
        target = path + (f"?{query}" if query else "")
        response = self.http.request(method, target, headers=headers, content=body)
        self.log.append(Exchange(method, target, headers, body, response.status_code,
                                 dict(response.headers), response.content))
        return response

    def _nonce(self) -> str:
        sig = guard_tag(self.key, "GET", self.host, "/__nonce", "", b"")
        response = self._send("GET", "/__nonce", headers={"WebSpec-Guard": sig})
        response.raise_for_status()
        return response.json()["nonce"]

    def _signed(self, level: int, method: str, path: str, query: str = "", body: bytes = b"",
                headers: dict[str, str] | None = None) -> Any:
        headers = dict(headers or {})
        if level >= 1:
            nonce = self._nonce()
            headers["WebSpec-Nonce"] = nonce
            headers["WebSpec-Guard"] = guard_tag(  # GD-2 signs the path with its slashes trimmed
                self.key, method, self.host, "/" + path.strip("/"), nonce, body, query,
                headers.get("WebSpec-Definer", ""), headers.get("Idempotency-Key", ""))
        return self._send(method, path, query, body, headers)

    def discover(self) -> dict:
        """OPTIONS /: the destination's level and its whole route map, one entry per key (SH-2, DS-1)."""
        if self._map is None:
            response = self._send("OPTIONS", "/")
            if response.status_code == 401:  # a guarded destination: sign and ask again
                response = self._signed(1, "OPTIONS", "/")
            response.raise_for_status()
            self._map = response.json()
        return self._map

    def entry(self, method: str, path: str, definer: str = "") -> dict | None:
        """The map's entry for ``method`` on ``path``, and on POST for ``definer``; None if none.

        An entry carries the tool it resolves to, its contract and the requirements at the
        destination's level; an entry whose key is closed carries only its ``error`` (DS-1).
        """
        method, noun = method.upper(), "/" + path.strip("/")
        for entry in self.discover()["entries"]:
            if entry["path"] == noun and entry["method"] == method and (
                    method != "POST" or entry.get("definer") == definer.upper()):
                return entry
        return None

    def call(self, method: str, path: str, args: dict | None = None, definer: str | None = None, *,
             approval: str | None = None, idempotency_key: str | None = None) -> Any:
        """Send the request the model wrote; add what the destination's level requires for its entry."""
        args = args or {}
        method = method.upper()
        level = self.discover()["level"]
        entry = self.entry(method, path, definer or "") or {}
        tool = entry.get("tool")  # None if the map has no open entry: the gateway will refuse it
        needs = entry.get("requirements", {})

        if method in ("GET", "DELETE"):
            query = urlencode({k: v if isinstance(v, str) else json.dumps(v) for k, v in args.items()},
                              quote_via=quote)
            body = b""
        else:
            query, body = "", json.dumps(args, separators=(",", ":")).encode()

        headers: dict[str, str] = {}
        if body:
            headers["Content-Type"] = "application/json"
        if method not in SAFE and definer:
            headers["WebSpec-Definer"] = definer.upper()  # DF-1: required on POST, optional otherwise
        if method not in SAFE and (needs.get("idempotency_key") or idempotency_key):
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        if needs.get("clearance") and self.vouch(method, tool, args):
            # CL-1 binds the tool the request resolves to, which the path alone may not name.
            headers["WebSpec-Clearance"] = clearance(self.key, self.destination, method, tool, args)
        if approval:
            headers["WebSpec-Approval"] = approval
        return self._signed(level, method, path, query, body, headers)
