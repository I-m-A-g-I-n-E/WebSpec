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

from .config import GuardKeyError, ServiceRegistry, get_session_key, is_public_host
from . import audit
from .guard import GuardError, duplicate_query_keys, generate_nonce, validate_guard
from .handlers import (
    handle_index,
    handle_service_head,
    handle_service_invoke,
    handle_service_list,
    handle_service_options,
    refuse_without_guard_key,
)
from .hostgrammar import qualifiers_allowed, split_labels
from .methods import ALL_METHODS
from .pool import ConnectionPool

logger = logging.getLogger("webspec")


def cors_origins() -> list[str]:
    """Explicit CORS allowlist from WEBSPEC_CORS_ORIGINS (comma-separated). Empty by default."""
    raw = os.environ.get("WEBSPEC_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


# Module-level singletons (initialized in create_app)
registry: ServiceRegistry | None = None
pool: ConnectionPool | None = None
_reload_task: asyncio.Task | None = None

CONFIG_POLL_INTERVAL = 30  # seconds


async def _config_reload_loop() -> None:
    """Periodically check ~/.claude.json for changes (F4).

    A file that cannot be loaded is rejected by the registry, which warns once and keeps the
    last good services (ServiceRegistry.reload). Anything else that fails here is logged as a
    WARNING, never below what the gateway logs by default: an edit that silently does not
    apply, a revocation included, looks applied to the operator.
    """
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
                        # Contract pins are deliberately kept: removing and re-adding a
                        # service must not be a way to reset them (methods.ContractPins).
                    # Modified/added services will be lazily re-created
                    for added in new_names - old_names:
                        logger.info("Config reload: new service available: %s", added)
                    logger.info("Config reloaded. Services: %s", registry.names())
        except Exception:
            logger.warning("Config reload check failed", exc_info=True)


# ── Route handlers that pull service from Host() match ──


def _deny(request: Request, service: str, status: int, error: str, detail: str) -> JSONResponse:
    """Refuse before any tool is resolved — and audit it (spec §11: every decision)."""
    audit.record_request(request, service=service, outcome="denied", status=status, reason=error)
    return JSONResponse({"error": error, "detail": detail}, status_code=status)


async def _service_dispatch(request: Request) -> Response:
    """Dispatch to the correct handler based on HTTP method for a service subdomain."""
    method = request.method.upper()
    path = request.path_params.get("path", "").strip("/")
    captured = request.path_params.get("service", "")

    # Host grammar: {qualifier}*.{destination}.{domain}. Only the destination routes.
    parsed = split_labels(captured)
    if parsed is None:
        return _deny(request, captured, 404, "invalid_host", "Host labels must be lowercase DNS labels.")
    service, qualifiers = parsed

    # One argument, one value: with a repeated key the tool would see only one of the
    # values, so no signature or fingerprint could bind the argument actually used.
    dupes = duplicate_query_keys(request.url.query)
    if dupes:
        return _deny(request, service, 400, "duplicate_query_key",
                     f"Query keys must not repeat ({', '.join(dupes)}); encode lists as JSON.")

    if registry is not None:
        entry = registry.get(service)
        host_header = request.headers.get("host", "")
        if qualifiers:
            if entry is None or not qualifiers_allowed(qualifiers, entry.labels):
                return _deny(request, service, 404, "unknown_qualifier",
                             "These qualifier labels are not allowed for this destination.")
            # Allowed by grammar, but per-qualifier backends are Proposed (C). Refuse rather
            # than silently serve every qualifier from the same backend (a false sense of
            # region/residency routing).
            return _deny(request, service, 404, "qualifier_not_routable",
                         "Qualifier labels are recognized but do not route to a separate backend yet.")
        if entry is not None and not entry.guard and is_public_host(host_header):
            return _deny(request, service, 403, "unguarded_public",
                         "Unguarded services are not exposed on the public domain.")
        if entry is not None and entry.guard:
            # Handle /__nonce bootstrap endpoint
            if path == "__nonce" and method == "GET":
                return await _handle_nonce(request, service)

            # Retired endpoint (was: dangerous-tier confirmation that nothing verified)
            if path == "__challenge" and method == "GET":
                return await _handle_challenge(request, service)

            # Enforce guard on all other requests. It signs the request line, body, query,
            # definer verb and Idempotency-Key, so none can be swapped in flight.
            body = await request.body()
            try:
                session_key = get_session_key()
            except GuardKeyError as exc:
                return refuse_without_guard_key(request, service, exc)
            guard_result = validate_guard(
                session_key=session_key,
                method=method,
                host=host_header,
                path=f"/{path}" if path else "/",
                body=body,
                guard_header=request.headers.get("X-WebSpec-Guard"),
                nonce_header=request.headers.get("X-WebSpec-Nonce"),
                query=request.url.query,
                audience=service,
                definer=request.headers.get("X-Gimme-Definer", ""),
                idempotency_key=request.headers.get("Idempotency-Key", ""),
            )
            if isinstance(guard_result, GuardError):
                return _deny(request, service, guard_result.status_code, guard_result.error_type, guard_result.detail)

    if method == "HEAD":
        return await handle_service_head(request, service, pool, registry)
    if method == "OPTIONS":
        return await handle_service_options(request, service, pool, registry)
    if method == "GET" and not path:
        return await handle_service_list(request, service, pool, registry)
    # Starlette's Route(methods=ALL_METHODS) has already refused anything else with 405.
    return await handle_service_invoke(request, service, pool, registry)


async def _handle_nonce(request: Request, service: str) -> Response:
    """Handle GET /__nonce — bootstrap a nonce with HMAC-only auth."""
    host = request.headers.get("host", "")
    try:
        session_key = get_session_key()
    except GuardKeyError as exc:
        # Audited like a failed guard on this endpoint (issued nonces are not audited, AU-1).
        return refuse_without_guard_key(request, service, exc)
    guard_result = validate_guard(
        session_key=session_key,
        method="GET",
        host=host,
        path="/__nonce",
        body=b"",
        guard_header=request.headers.get("X-WebSpec-Guard"),
        nonce_header=None,
        is_nonce_request=True,
    )
    if isinstance(guard_result, GuardError):
        return _deny(request, service, guard_result.status_code, guard_result.error_type, guard_result.detail)
    audience = service
    nonce_data = generate_nonce(audience)
    return JSONResponse(nonce_data)


async def _handle_challenge(request: Request, service: str) -> Response:
    """Retired: the old /__challenge minted a challenge that nothing ever verified.

    Human confirmation is now the level-4 approval flow: the gateway answers the actual
    request with 428 + a signed-summary challenge (see approval.py).
    """
    return JSONResponse(
        {"error": "gone",
         "detail": "/__challenge is retired. Level-4 services answer the real request with "
                   "428 Precondition Required and an approval challenge; see "
                   "docs/spec/levels.md (Level 4: witnessed)."},
        status_code=410,
    )


async def _index_dispatch(request: Request) -> Response:
    """Handle requests to bare localhost:7001."""
    return await handle_index(request, registry, pool)


# ── Service sub-app routes ──

service_routes = Starlette(
    routes=[
        Route("/", _service_dispatch, methods=list(ALL_METHODS)),
        Route("/{path:path}", _service_dispatch, methods=list(ALL_METHODS)),
    ],
)

index_routes = Starlette(
    routes=[
        Route("/", _index_dispatch, methods=["GET", "HEAD"]),
    ],
)


def _report_guard_key() -> None:
    """GD-5: say once, at startup, that requests needing the guard key will be refused.

    The gateway still starts: the key is read again for each request that needs it, so a
    key file fixed later takes effect at once. Without this, the only sign of a missing key
    was the 500 for each such request. GuardKeyError names the variable or the file only.
    """
    try:
        get_session_key()
    except GuardKeyError as exc:
        logger.warning("No usable guard key, so every guarded request and every unsafe request is "
                       "refused with a plain-text 500 until there is one (GD-5): %s", exc)


def create_app() -> Starlette:
    """Create the Starlette application with Host-based routing."""
    global registry, pool

    registry = ServiceRegistry()
    pool = ConnectionPool(registry)

    logger.info("Registered services: %s", registry.names())
    _report_guard_key()

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
                allow_methods=list(ALL_METHODS),
                allow_headers=["X-WebSpec-Guard", "X-WebSpec-Nonce", "X-Gimme-Definer",
                               "X-UFO-Clearance", "X-UFO-Provenance", "X-WebSpec-Approval",
                               "Idempotency-Key", "Content-Type"],
            ),
        ],
        lifespan=lifespan,
    )

    return app
