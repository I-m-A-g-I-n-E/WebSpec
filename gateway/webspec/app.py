"""Starlette application with Host() wildcard subdomain routing."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Host, Route, request_response

from .config import GuardKeyError, ServiceRegistry, get_session_key, is_public_host
from . import audit
from .definer import DEFINER_HEADER, definer_header
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
                        # Contract pins and route maps (binding pins, moved tools) are
                        # deliberately kept: removing and re-adding a service must not be a
                        # way to reset them (TC-6, PA-8).
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
    # Starlette 1.9 matches Host() routes case-insensitively and captures the labels in lower
    # case, so check the header as sent: HG-2 admits lowercase labels only, and the checks
    # below (public exposure, the guard) read the header itself.
    parsed = split_labels(captured)
    host_sent = request.headers.get("host", "")
    if parsed is None or host_sent != host_sent.lower():
        return _deny(request, captured, 404, "invalid_host", "Host labels must be lowercase DNS labels.")
    service, qualifiers = parsed

    # One argument, one value: with a repeated key the tool would see only one of the
    # values, so no signature or fingerprint could bind the argument actually used.
    dupes = duplicate_query_keys(request.url.query)
    if dupes:
        return _deny(request, service, 400, "duplicate_query_key",
                     f"Query keys must not repeat ({', '.join(dupes)}); encode lists as JSON.")
    # DF-2: at most one definer field line, whatever the method, refused before the guard.
    if len(request.headers.getlist(DEFINER_HEADER)) > 1:
        return _deny(request, service, 400, "unknown_definer", f"Send at most one {DEFINER_HEADER} field line.")

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
                guard_header=request.headers.get("WebSpec-Guard"),
                nonce_header=request.headers.get("WebSpec-Nonce"),
                query=request.url.query,
                audience=service,
                definer=definer_header(request.headers) or "",
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
    # Every other method, TRACE and extension methods included, is resolved on the route map
    # (PA-10), which answers a method it does not map with 405 and the path's Allow.
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
        guard_header=request.headers.get("WebSpec-Guard"),
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

class _EveryMethod:
    """An endpoint as an ASGI app, so that Route() passes it every method, TRACE included, and
    the gateway answers each one itself (PA-10). Route() limits a function endpoint to the
    methods it lists."""

    def __init__(self, endpoint) -> None:
        self.app = request_response(endpoint)

    async def __call__(self, scope, receive, send) -> None:
        await self.app(scope, receive, send)


service_routes = Starlette(
    routes=[
        Route("/", _EveryMethod(_service_dispatch)),
        Route("/{path:path}", _EveryMethod(_service_dispatch)),
    ],
)


class NoStore:
    """``Cache-Control: no-store`` on every response that sets none (RS-4, DS-3, GD-4).

    That covers refusals, the web framework's own included (a host that matches no route, a
    rejected CORS preflight), discovery answers and nonces: a cache must not keep a 404 or
    a 405 after the route map changes, and nothing else the gateway serves is cacheable.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_no_store(message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault("cache-control", "no-store")
            await send(message)

        await self.app(scope, receive, send_no_store)


def _internal_error(request: Request, exc: Exception) -> Response:
    """The plain-text 500 for an unhandled error, which is answered outside every middleware."""
    return PlainTextResponse("Internal Server Error", status_code=500, headers={"Cache-Control": "no-store"})


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
        logger.warning("No usable guard key, so every guarded request is refused with a plain-text 500 "
                       "until there is one (GD-5): %s", exc)


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
            Middleware(NoStore),
            # DP-8: what an origin in WEBSPEC_CORS_ORIGINS may send, and what it may read. Without
            # WebSpec-Taint in the exposed list a harness in that origin could not see the mark (RS-2).
            Middleware(
                CORSMiddleware,
                allow_origins=cors_origins(),
                allow_methods=list(ALL_METHODS),
                allow_headers=[DEFINER_HEADER, "WebSpec-Guard", "WebSpec-Nonce", "WebSpec-Clearance",
                               "WebSpec-Approval", "WebSpec-Provenance", "Idempotency-Key", "Content-Type"],
                expose_headers=["Allow", "Link", "WebSpec-Level", "WebSpec-Tier", "WebSpec-Taint",
                                "WebSpec-Status", "WebSpec-Tool-Count", "WebSpec-Service", "Idempotent-Replayed"],
            ),
        ],
        exception_handlers={Exception: _internal_error},
        lifespan=lifespan,
    )

    return app
