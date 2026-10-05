"""Test harness: the real Starlette app over a fake registry + fake MCP pool.

Documented mock exception (AGENTS.md: "mocks only by documented exception"): FakePool
stands in for MCP servers here because these tests need deterministic fault injection
(timeouts, protocol rejections, unserializable results) and exact control over tool
annotations, which a live server cannot provide. The real path — a stdio FastMCP server
through the real ConnectionPool — is covered without mocks in test_stdio_integration.py.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from starlette.testclient import TestClient

import webspec.app as appmod
from webspec.config import ServiceEntry
from webspec.definer import compute_bookend_hash
from webspec.guard import canonical_query, compute_clearance_token, compute_guard_hmac

GUARD_KEY_HEX = "11" * 32
KEY = bytes.fromhex(GUARD_KEY_HEX)
HAVE_SSH_KEYGEN = shutil.which("ssh-keygen") is not None


def tool(name: str, *, read_only=None, destructive=None, idempotent=None, open_world=None,
         tier: str | None = None, annotated: bool = True) -> Tool:
    ann = None
    if annotated:
        ann = ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive,
                              idempotentHint=idempotent, openWorldHint=open_world)
    kwargs = {"_meta": {"webspec/tier": tier}} if tier else {}
    return Tool(name=name, description=f"{name} tool", inputSchema={"type": "object"}, annotations=ann, **kwargs)


class FakeRegistry:
    def __init__(self, entries: list[ServiceEntry]):
        self._entries = {e.name: e for e in entries}

    def get(self, name):
        return self._entries.get(name)

    def names(self):
        return list(self._entries)

    @property
    def services(self):
        return self._entries


class FakePool:
    def __init__(self, tools: list[Tool]):
        self.tools = tools
        self.calls: list[tuple[str, str, dict]] = []
        self.raise_on_call: BaseException | None = None

    async def list_tools(self, name):
        return self.tools

    async def ping(self, name):
        return True

    async def call_tool(self, name, tool_name, arguments=None, timeout=30.0):
        self.calls.append((name, tool_name, dict(arguments or {})))
        if self.raise_on_call is not None:
            raise self.raise_on_call
        await asyncio.sleep(0)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": tool_name}))],
                              isError=False)

    def connected_services(self):
        return []


def make_client(monkeypatch, entries: list[ServiceEntry], tools: list[Tool]) -> tuple[TestClient, FakePool]:
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", GUARD_KEY_HEX)
    monkeypatch.delenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", raising=False)
    monkeypatch.delenv("WEBSPEC_DOMAIN", raising=False)
    app = appmod.create_app()
    pool = FakePool(tools)
    appmod.registry = FakeRegistry(entries)
    appmod.pool = pool
    return TestClient(app), pool


def entry(name: str = "svc", *, level: int = 0, tools: dict | None = None, labels=()) -> ServiceEntry:
    return ServiceEntry(name=name, original_name=name, transport_type="stdio", command="x",
                        guard=level >= 1, level=level, tools=tools or {}, labels=tuple(labels))


def bookend(method: str, verb: str, body: bytes) -> str:
    return f"{verb}:{compute_bookend_hash(KEY, method, verb, body)}"


def clearance(tool_name: str, args: dict, *, service: str = "svc", method: str, ts: int | None = None) -> str:
    import time
    ts = int(time.time()) if ts is None else ts
    token = compute_clearance_token(KEY, tool_name, args, str(ts), service=service, method=method)
    return f"{token}:{ts}"


def request(client: TestClient, method: str, host: str, path: str, *, query: str = "",
            body: bytes = b"", headers: dict | None = None, guarded: bool = False,
            sign_query: str | None = None):
    """Send a request; if ``guarded``, bootstrap a nonce and sign it like a real client."""
    hdrs = {"Host": host, **(headers or {})}
    if guarded:
        boot_mac = compute_guard_hmac(KEY, "GET", host, "/__nonce", "", b"")
        r = client.get("/__nonce", headers={"Host": host, "X-WebSpec-Guard": boot_mac})
        assert r.status_code == 200, r.text
        nonce = r.json()["nonce"]
        q = canonical_query(query if sign_query is None else sign_query)
        hdrs["X-WebSpec-Guard"] = compute_guard_hmac(
            KEY, method, host, path, nonce, body, q,
            definer=hdrs.get("X-Gimme-Definer", ""), idempotency_key=hdrs.get("Idempotency-Key", ""))
        hdrs["X-WebSpec-Nonce"] = nonce
    url = path + (f"?{query}" if query else "")
    return client.request(method, url, headers=hdrs, content=body)


def make_approver(tmp_path: Path) -> tuple[Path, Path]:
    """Generate an Ed25519 approver key + allowed_signers file restricted to our namespace."""
    key = tmp_path / "approver"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "approver", "-f", str(key)], check=True)
    pub = (tmp_path / "approver.pub").read_text().split()
    signers = tmp_path / "allowed_signers"
    signers.write_text(f'approver@test namespaces="webspec-approval" {pub[0]} {pub[1]}\n')
    return key, signers


def ssh_sign(key: Path, message: str) -> str:
    from webspec.approval import APPROVAL_NAMESPACE, dearmor
    out = subprocess.run(["ssh-keygen", "-Y", "sign", "-n", APPROVAL_NAMESPACE, "-f", str(key)],
                         input=message.encode(), capture_output=True, check=True).stdout.decode()
    return dearmor(out)


