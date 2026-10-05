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
from starlette.responses import JSONResponse, Response
from starlette.routing import Host, Route

from .config import ServiceRegistry, get_session_key
from .guard import GuardError, generate_nonce, validate_guard, validate_provenance_chain
from .handlers import (
    handle_index,
    handle_service_get,
    handle_service_head,
    handle_service_mutate,
    handle_service_options,
)
from .pool import ConnectionPool

logger = logging.getLogger("webspec")


def cors_origins() -> list[str]:
    """Explicit CORS allowlist from WEBSPEC_CORS_ORIGINS (comma-separated). Empty by default."""
    raw = os.environ.get("WEBSPEC_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


def _is_public_host(host: str) -> bool:
    """True if the Host header targets the configured public domain (not localhost)."""
    public_domain = os.environ.get("WEBSPEC_DOMAIN")
    if not public_domain:
        return False
    hostname = host.split(":")[0]
    return hostname == public_domain or hostname.endswith("." + public_domain)


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
    path = request.path_params.get("path", "").strip("/")

    # Check if this service requires guard authentication
    if registry is not None:
        entry = registry.get(service)
        host_header = request.headers.get("host", "")
        if entry is not None and not entry.guard and _is_public_host(host_header):
            return JSONResponse(
                {"error": "unguarded_public",
                 "detail": "Unguarded services are not exposed on the public domain."},
                status_code=403,
            )
        if entry is not None and entry.guard:
            # Handle /__nonce bootstrap endpoint
            if path == "__nonce" and method == "GET":
                return await _handle_nonce(request, service)

            # Handle /__challenge endpoint for dangerous-tier human confirmation
            if path == "__challenge" and method == "GET":
                return await _handle_challenge(request, service)

            # Enforce guard on all other requests
            body = await request.body()
            host = request.headers.get("host", "")
            guard_result = validate_guard(
                session_key=get_session_key(),
                method=method,
                host=host,
                path=f"/{path}" if path else "/",
                body=body,
                guard_header=request.headers.get("X-WebSpec-Guard"),
                nonce_header=request.headers.get("X-WebSpec-Nonce"),
            )
            if isinstance(guard_result, GuardError):
                return JSONResponse(
                    {"error": guard_result.error_type, "detail": guard_result.detail},
                    status_code=guard_result.status_code,
                )

            # Store UFO headers on request state for downstream use
            request.state.ufo_clearance = request.headers.get("X-UFO-Clearance")
            request.state.ufo_provenance = request.headers.get("X-UFO-Provenance")

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


async def _handle_nonce(request: Request, service: str) -> Response:
    """Handle GET /__nonce — bootstrap a nonce with HMAC-only auth."""
    host = request.headers.get("host", "")
    guard_result = validate_guard(
        session_key=get_session_key(),
        method="GET",
        host=host,
        path="/__nonce",
        body=b"",
        guard_header=request.headers.get("X-WebSpec-Guard"),
        nonce_header=None,
        is_nonce_request=True,
    )
    if isinstance(guard_result, GuardError):
        return JSONResponse(
            {"error": guard_result.error_type, "detail": guard_result.detail},
            status_code=guard_result.status_code,
        )
    audience = service
    nonce_data = generate_nonce(audience)
    return JSONResponse(nonce_data)


async def _handle_challenge(request: Request, service: str) -> Response:
    """Handle GET /__challenge — issue a human confirmation challenge for dangerous-tier tools."""
    host = request.headers.get("host", "")
    guard_result = validate_guard(
        session_key=get_session_key(),
        method="GET",
        host=host,
        path="/__challenge",
        body=b"",
        guard_header=request.headers.get("X-WebSpec-Guard"),
        nonce_header=None,
        is_nonce_request=True,  # challenge bootstrap works like nonce bootstrap
    )
    if isinstance(guard_result, GuardError):
        return JSONResponse(
            {"error": guard_result.error_type, "detail": guard_result.detail},
            status_code=guard_result.status_code,
        )

    import secrets as _secrets
    challenge = _secrets.token_hex(16)
    tool = request.query_params.get("tool", "")
    return JSONResponse({
        "challenge": challenge,
        "service": service,
        "tool": tool,
        "message": f"Confirm: execute '{tool}' on {service}?",
        "expires_in": 30,
    })


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
    # WEBSPEC_PORT is the public-facing port (Caddy, default 7001)
    # WEBSPEC_INTERNAL_PORT is the gateway listen port (default 7002)
    # Both ports are matched in Host() routes so requests work regardless of entry point
    public_port = os.environ.get("WEBSPEC_PORT", "7001")
    internal_port = os.environ.get("WEBSPEC_INTERNAL_PORT", "7002")

    routes = [
        Host("{service}.localhost", app=service_routes, name="service"),
        Host(f"{{service}}.localhost:{public_port}", app=service_routes, name="service_public_port"),
        Host(f"{{service}}.localhost:{internal_port}", app=service_routes, name="service_internal_port"),
        Host("localhost", app=index_routes, name="index"),
        Host(f"localhost:{public_port}", app=index_routes, name="index_public_port"),
        Host(f"localhost:{internal_port}", app=index_routes, name="index_internal_port"),
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
                allow_origins=cors_origins(),
                allow_methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH"],
                allow_headers=["X-WebSpec-Guard", "X-WebSpec-Nonce", "X-Gimme-Definer",
                               "X-UFO-Clearance", "X-UFO-Provenance", "Content-Type"],
            ),
        ],
        lifespan=lifespan,
    )

    return app
