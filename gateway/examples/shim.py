"""A reference harness shim for WebSpec (spec: docs/spec/levels.md, "The harness shim").

The model decides only the method, the tool, the arguments and one definer verb. The shim
adds everything cryptographic — nonce, guard HMAC, bookend, Idempotency-Key, clearance — as
the destination's level requires, which it learns from ``OPTIONS /{tool}``. It holds the
guard key; the model never sees it.

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


def bookend(key: bytes, method: str, verb: str, body: bytes) -> str:
    """DF-2: HMAC over the method, the verb, and the first and last 16 bytes of the body."""
    head, tail = (body, body) if len(body) <= 16 else (body[:16], body[-16:])
    return tag(key, f"{method}:{verb}:".encode() + head + b":" + tail)


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
    _info: dict[str, dict] = field(default_factory=dict)

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
        response = self._send("GET", "/__nonce", headers={"X-WebSpec-Guard": sig})
        response.raise_for_status()
        return response.json()["nonce"]

    def _signed(self, level: int, method: str, path: str, query: str = "", body: bytes = b"",
                headers: dict[str, str] | None = None) -> Any:
        headers = dict(headers or {})
        if level >= 1:
            nonce = self._nonce()
            headers["X-WebSpec-Nonce"] = nonce
            headers["X-WebSpec-Guard"] = guard_tag(
                self.key, method, self.host, path, nonce, body, query,
                headers.get("X-Gimme-Definer", ""), headers.get("Idempotency-Key", ""))
        return self._send(method, path, query, body, headers)

    def discover(self, tool: str) -> dict:
        """OPTIONS /{tool}: contract, methods, level and per-method requirements (SH-2)."""
        if tool not in self._info:
            response = self._send("OPTIONS", f"/{tool}")
            if response.status_code == 401:  # a guarded destination: sign and ask again
                response = self._signed(1, "OPTIONS", f"/{tool}")
            response.raise_for_status()
            self._info[tool] = response.json()
        return self._info[tool]

    def call(self, method: str, tool: str, args: dict | None = None, definer: str | None = None, *,
             approval: str | None = None, idempotency_key: str | None = None) -> Any:
        """Invoke ``tool`` the way the model asked; add what the destination's level requires."""
        args = args or {}
        info = self.discover(tool)
        level = info["level"]
        needs = info["requirements"].get(method.upper(), {})  # empty if the method isn't admissible
        method = method.upper()

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
            verb = definer.upper()
            headers["X-Gimme-Definer"] = f"{verb}:{bookend(self.key, method, verb, body)}" if needs.get("bookend") else verb
        if method not in SAFE and (needs.get("idempotency_key") or idempotency_key):
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        if needs.get("clearance") and self.vouch(method, tool, args):
            headers["X-UFO-Clearance"] = clearance(self.key, self.destination, method, tool, args)
        if approval:
            headers["X-WebSpec-Approval"] = approval
        return self._signed(level, method, f"/{tool}", query, body, headers)
