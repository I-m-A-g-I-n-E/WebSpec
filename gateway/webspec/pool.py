"""Connection pool: lazy FastMCP Client lifecycle with reconnection."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field

from fastmcp.client import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED, Tool

from .config import ServiceEntry, ServiceRegistry

logger = logging.getLogger("webspec.pool")

TOOL_CACHE_TTL = 300  # 5 minutes
DEFAULT_TIMEOUT = 30  # seconds

# Set for a stdio server, unless its entry's ``env`` sets the same name, when the gateway
# itself ignores user site-packages. The MCP client passes a stdio server only HOME, PATH and
# a few other variables plus the entry's env, so the gateway's own PYTHONNOUSERSITE (or
# ``python -I``) does not reach it. A stdio server's HOME can be writable by the other stdio
# servers (in the Docker stack it is the state volume; on the hardened units, the
# gateway's HOME), and Python runs any .pth file or usercustomize module in HOME's user
# site-packages at startup: one server could plant code that then runs, at each start, in
# every Python stdio server, next to that server's secrets. Defense in depth next to DP-4.
# An entry that needs user site-packages sets "PYTHONNOUSERSITE": "" (empty means unset).
STDIO_ENV_DEFAULTS = {"PYTHONNOUSERSITE": "1"}


def _ignores_user_site() -> bool:
    """Whether this gateway ignores user site-packages: ``python -I`` or ``-s``, or PYTHONNOUSERSITE.

    Every shipped production launcher starts the gateway with ``-I`` (and the Docker image
    sets PYTHONNOUSERSITE too). A development gateway started without them, as the units in
    gateway/systemd and gateway/launchd do, may itself need user site-packages, and so may
    its stdio servers (``pip install --user``); their HOME is the agent's own, which the
    agent can write anyway.
    """
    return bool(sys.flags.no_user_site)


def stdio_env(env):
    """The ``env`` a stdio server is spawned with.

    When the gateway ignores user site-packages, STDIO_ENV_DEFAULTS, then the entry's own env;
    otherwise the entry's env alone, as before. A malformed env (not a mapping) is passed on
    unchanged, so that server fails when it is spawned, as before: configuration is
    validated shallowly.
    """
    if not env:
        env = {}
    if not isinstance(env, dict):
        return env
    if _ignores_user_site():
        return {**STDIO_ENV_DEFAULTS, **env}
    return env or None


# A stdio server's working directory when the gateway keeps its own off sys.path. The MCP
# client starts a server in the gateway's working directory, and ``python -m`` and
# ``python -c`` put that directory first on sys.path. The hardened units run the gateway in
# its HOME, /var/lib/webspec, which every stdio server can write (they all run as the
# gateway's user): one server could plant a module there that then runs, at each start,
# inside every ``python -m`` server, next to that server's secrets. Only root can write /,
# the usual working directory of a daemon. Defense in depth next to DP-4. PYTHONSAFEPATH
# would not do: it also drops a script's own directory from sys.path, and servers import
# their own modules from there (services/op-auth does).
STDIO_ISOLATED_CWD = "/"


def _ignores_working_directory() -> bool:
    """Whether this gateway keeps its working directory off sys.path: ``python -I`` or ``-P``, or PYTHONSAFEPATH.

    Every shipped production launcher starts the gateway with ``-I``; the development units
    in gateway/systemd and gateway/launchd do not. sys.flags.safe_path is new in Python 3.11:
    a development unit's python3 may be older, and before 3.11 nothing keeps the working
    directory off sys.path, so such a gateway starts its stdio servers as before.
    """
    return bool(getattr(sys.flags, "safe_path", False))


def stdio_cwd() -> str | None:
    """The working directory a stdio server is spawned in.

    STDIO_ISOLATED_CWD when the gateway keeps its own working directory off sys.path;
    otherwise None, the gateway's own, as before.
    """
    return STDIO_ISOLATED_CWD if _ignores_working_directory() else None


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
                env=stdio_env(entry.env),
                cwd=stdio_cwd(),
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

            try:
                pooled.tools = await client.list_tools()
            except Exception:
                # A dead connection would otherwise keep failing until a restart: drop it, and
                # let the next request reconnect.
                await self._teardown(name)
                raise
            pooled.tools_fetched_at = now
            return pooled.tools


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
        except McpError as e:
            if e.error.code == CONNECTION_CLOSED:
                # The server went away mid-call (e.g. a stdio server crashed): this client is dead.
                logger.warning("Connection to %s closed during %s", name, tool_name)
                await self._teardown(name)
            raise
        except Exception as e:
            # Connection errors, closed or broken streams, anything else: the client may be
            # unusable, so drop it and let the next call reconnect (F1).
            logger.warning("Error calling %s/%s: %s", name, tool_name, e)
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
