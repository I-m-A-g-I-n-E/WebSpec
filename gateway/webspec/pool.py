"""Connection pool: lazy FastMCP Client lifecycle with reconnection."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from fastmcp.client import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from mcp.types import Tool

from .config import ServiceEntry, ServiceRegistry

logger = logging.getLogger("webspec.pool")

TOOL_CACHE_TTL = 300  # 5 minutes
DEFAULT_TIMEOUT = 30  # seconds


@dataclass
class PooledService:
    """A pooled MCP client with cached tool list."""
    client: Client
    tools: list[Tool] = field(default_factory=list)
    tools_fetched_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConnectionPool:
    """Manages lazy FastMCP Client connections per service."""

    def __init__(self, registry: ServiceRegistry):
        self._registry = registry
        self._pool: dict[str, PooledService] = {}
        self._init_locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def _create_client(self, entry: ServiceEntry) -> Client:
        """Create a FastMCP Client for the given service entry."""
        if entry.transport_type == "http":
            transport = StreamableHttpTransport(
                url=entry.url,
                headers=entry.headers or None,
            )
        else:
            transport = StdioTransport(
                command=entry.command,
                args=entry.args,
                env=entry.env or None,
                keep_alive=True,
            )
        return Client(transport, name=f"webspec-{entry.name}")

    async def _get_init_lock(self, name: str) -> asyncio.Lock:
        """Get or create a per-service initialization lock."""
        async with self._global_lock:
            if name not in self._init_locks:
                self._init_locks[name] = asyncio.Lock()
            return self._init_locks[name]

    async def get_client(self, name: str) -> Client:
        """Get a connected client for the named service. Creates lazily."""
        entry = self._registry.get(name)
        if entry is None:
            raise KeyError(f"Unknown service: {name}")

        # Fast path: already connected
        if name in self._pool:
            pooled = self._pool[name]
            if pooled.client.is_connected():
                return pooled.client
            # Stale — remove and recreate
            logger.info("Service %s disconnected, recreating", name)
            await self._teardown(name)

        # Slow path: initialize under lock
        lock = await self._get_init_lock(name)
        async with lock:
            # Double-check after acquiring lock
            if name in self._pool and self._pool[name].client.is_connected():
                return self._pool[name].client

            client = self._create_client(entry)
            await client.__aenter__()
            self._pool[name] = PooledService(client=client)
            logger.info("Connected to service: %s (%s)", name, entry.transport_type)
            return client

    async def list_tools(self, name: str) -> list[Tool]:
        """Get cached tool list for a service, refreshing if stale."""
        client = await self.get_client(name)

        pooled = self._pool[name]
        async with pooled.lock:
            now = time.monotonic()
            if pooled.tools and (now - pooled.tools_fetched_at) < TOOL_CACHE_TTL:
                return pooled.tools

            pooled.tools = await client.list_tools()
            pooled.tools_fetched_at = now
            return pooled.tools

    async def find_tool(self, name: str, tool_name: str) -> Tool | None:
        """Find a specific tool by name within a service."""
        tools = await self.list_tools(name)
        for tool in tools:
            if tool.name == tool_name:
                return tool
        return None

    async def call_tool(
        self, name: str, tool_name: str, arguments: dict | None = None, timeout: float = DEFAULT_TIMEOUT
    ):
        """Call a tool on a service with timeout protection."""
        client = await self.get_client(name)
        try:
            result = await asyncio.wait_for(
                client.call_tool_mcp(tool_name, arguments or {}),
                timeout=timeout,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning("Tool call timed out: %s/%s after %ss", name, tool_name, timeout)
            # Teardown the connection on timeout (F2)
            await self._teardown(name)
            raise
        except (ConnectionError, OSError) as e:
            logger.warning("Connection error calling %s/%s: %s", name, tool_name, e)
            # Teardown on connection error (F1)
            await self._teardown(name)
            raise

    async def ping(self, name: str) -> bool:
        """Ping a service. Returns True if healthy."""
        try:
            client = await self.get_client(name)
            return await client.ping()
        except Exception:
            return False

    async def _teardown(self, name: str) -> None:
        """Tear down a pooled client."""
        pooled = self._pool.pop(name, None)
        if pooled is not None:
            try:
                await pooled.client.close()
            except Exception:
                logger.debug("Error closing client for %s", name, exc_info=True)

    async def remove_service(self, name: str) -> None:
        """Remove a service from the pool (e.g., on config reload)."""
        await self._teardown(name)

    async def close_all(self) -> None:
        """Shut down all pooled connections."""
        names = list(self._pool.keys())
        for name in names:
            await self._teardown(name)

    def connected_services(self) -> list[str]:
        """Return names of currently connected services."""
        return [name for name, p in self._pool.items() if p.client.is_connected()]
