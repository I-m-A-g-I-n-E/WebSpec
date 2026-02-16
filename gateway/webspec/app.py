"""Starlette application with Host() wildcard subdomain routing."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Host, Route

from .config import ServiceRegistry
from .handlers import (
    handle_index,
    handle_service_get,
    handle_service_head,
    handle_service_mutate,
    handle_service_options,
)
from .pool import ConnectionPool

logger = logging.getLogger("webspec")

# Module-level singletons (initialized in create_app)
registry: ServiceRegistry | None = None
pool: ConnectionPool | None = None
_reload_task: asyncio.Task | None = None

CONFIG_POLL_INTERVAL = 30  # seconds


async def _config_reload_loop() -> None:
    """Periodically check ~/.claude.json for changes (F4)."""
    while True:
        await asyncio.sleep(CONFIG_POLL_INTERVAL)
        try:
            if registry is not None and pool is not None:
                old_names = set(registry.names())
                changed = registry.check_reload()
                if changed:
                    new_names = set(registry.names())
                    # Remove services that were deleted from config
                    for removed in old_names - new_names:
                        logger.info("Config reload: removing service %s", removed)
                        await pool.remove_service(removed)
                    # Modified/added services will be lazily re-created
                    for added in new_names - old_names:
                        logger.info("Config reload: new service available: %s", added)
                    logger.info("Config reloaded. Services: %s", registry.names())
        except Exception:
            logger.debug("Config reload check failed", exc_info=True)


# ── Route handlers that pull service from Host() match ──


async def _service_dispatch(request: Request) -> Response:
    """Dispatch to the correct handler based on HTTP method for a service subdomain."""
    service = request.path_params.get("service", "")
    method = request.method.upper()

    if method == "HEAD":
        return await handle_service_head(request, service, pool, registry)
    elif method == "OPTIONS":
        return await handle_service_options(request, service, pool, registry)
    elif method == "GET":
        return await handle_service_get(request, service, pool, registry)
    elif method in ("POST", "PUT", "PATCH"):
        return await handle_service_mutate(request, service, pool, registry)
    else:
        return Response(status_code=405)


async def _index_dispatch(request: Request) -> Response:
    """Handle requests to bare localhost:7001."""
    return await handle_index(request, registry, pool)


# ── Service sub-app routes ──

service_routes = Starlette(
    routes=[
        Route("/", _service_dispatch, methods=["HEAD", "OPTIONS", "GET", "POST", "PUT", "PATCH"]),
        Route("/{path:path}", _service_dispatch, methods=["HEAD", "OPTIONS", "GET", "POST", "PUT", "PATCH"]),
    ],
)

index_routes = Starlette(
    routes=[
        Route("/", _index_dispatch, methods=["GET", "HEAD"]),
    ],
)


def create_app() -> Starlette:
    """Create the Starlette application with Host-based routing."""
    global registry, pool

    registry = ServiceRegistry()
    pool = ConnectionPool(registry)

    logger.info("Registered services: %s", registry.names())

    @asynccontextmanager
    async def lifespan(app):
        global _reload_task
        _reload_task = asyncio.create_task(_config_reload_loop())
        logger.info("WebSpec gateway started. Config reload polling every %ds.", CONFIG_POLL_INTERVAL)
        yield
        if _reload_task is not None:
            _reload_task.cancel()
            _reload_task = None
        if pool is not None:
            await pool.close_all()
        logger.info("WebSpec gateway shut down.")

    # Build host routes — always include localhost, optionally a public domain
    routes = [
        Host("{service}.localhost", app=service_routes, name="service"),
        Host("{service}.localhost:7001", app=service_routes, name="service_with_port"),
        Host("localhost", app=index_routes, name="index"),
        Host("localhost:7001", app=index_routes, name="index_with_port"),
    ]

    # WEBSPEC_DOMAIN adds public-facing host patterns (e.g. "i-a-m.live")
    public_domain = os.environ.get("WEBSPEC_DOMAIN")
    if public_domain:
        routes.extend([
            Host(f"{{service}}.{public_domain}", app=service_routes, name="service_public"),
            Host(public_domain, app=index_routes, name="index_public"),
        ])
        logger.info("Public domain enabled: *.%s", public_domain)

    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
            ),
        ],
        lifespan=lifespan,
    )

    return app
