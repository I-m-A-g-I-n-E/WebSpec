"""HTTP method dispatch: HEAD/OPTIONS/GET/POST/PUT/PATCH → MCP operations."""

from __future__ import annotations

import asyncio
import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import ServiceRegistry, get_session_key
from .definer import DefinerError, DefinerResult, validate_definer
from .permissions import LOCAL_ALLOW_ALL, allowed_methods, authorize, scope_required
from .pool import ConnectionPool, DEFAULT_TIMEOUT
from .serializers import serialize_tool_result

logger = logging.getLogger("webspec.handlers")


def _error(status: int, error_type: str, detail: str, **extra) -> JSONResponse:
    body = {"error": error_type, "detail": detail, **extra}
    return JSONResponse(body, status_code=status)


def _resolve_tool_name(path: str, tool_names: list[str]) -> str | None:
    """Resolve path to tool name. Try exact match first, then slash-to-underscore fallback."""
    if path in tool_names:
        return path
    # Slash-to-underscore fallback (F10)
    alt = path.replace("/", "_")
    if alt in tool_names:
        return alt
    return None


async def _get_payload(request: Request) -> bytes:
    """Read request body."""
    return await request.body()


def _merge_arguments(request: Request, body: bytes) -> dict:
    """Merge query params + JSON body into tool arguments."""
    args: dict = {}
    # Query params
    for key, value in request.query_params.items():
        args[key] = value
    # JSON body (if present)
    if body:
        try:
            body_args = json.loads(body)
            if isinstance(body_args, dict):
                args.update(body_args)
        except (json.JSONDecodeError, TypeError):
            pass
    return args


# ── Service-level handlers (on {service}.localhost) ──


async def handle_service_head(request: Request, service: str, pool: ConnectionPool, registry: ServiceRegistry) -> Response:
    """HEAD / → ping; HEAD /{tool} → tool existence check."""
    path = request.path_params.get("path", "").strip("/")
    entry = registry.get(service)
    if entry is None:
        return _error(404, "unknown_service", f"Unknown service: {service}", available=registry.names())

    headers = {"X-WebSpec-Service": service}

    if not path:
        # HEAD / → ping + tool count
        try:
            tools = await pool.list_tools(service)
            healthy = await pool.ping(service)
            headers["X-WebSpec-Tool-Count"] = str(len(tools))
            headers["X-WebSpec-Status"] = "connected" if healthy else "degraded"
            # Add allowed/denied methods
            methods = allowed_methods(LOCAL_ALLOW_ALL, f"{service}.localhost", "")
            headers["X-Gimme-Allowed"] = ", ".join(methods)
            return Response(status_code=200, headers=headers)
        except Exception as e:
            logger.warning("HEAD %s failed: %s", service, e)
            return Response(status_code=503, headers={"Retry-After": "1", **headers})
    else:
        # HEAD /{tool} → tool existence check
        try:
            tool = await pool.find_tool(service, path)
            if tool is None:
                # Try slash-to-underscore
                alt = path.replace("/", "_")
                tool = await pool.find_tool(service, alt)
            if tool is None:
                return Response(status_code=404, headers=headers)
            headers["X-WebSpec-Tool"] = tool.name
            # Sanitize description for HTTP header (no newlines, control chars)
            desc = (tool.description or "")[:200]
            desc = " ".join(desc.split())  # collapse whitespace/newlines
            headers["X-WebSpec-Description"] = desc
            return Response(status_code=200, headers=headers)
        except Exception as e:
            logger.warning("HEAD %s/%s failed: %s", service, path, e)
            return Response(status_code=503, headers={"Retry-After": "1", **headers})


async def handle_service_options(request: Request, service: str, pool: ConnectionPool, registry: ServiceRegistry) -> Response:
    """OPTIONS / → all tools; OPTIONS /{tool} → single tool schema."""
    path = request.path_params.get("path", "").strip("/")
    entry = registry.get(service)
    if entry is None:
        return _error(404, "unknown_service", f"Unknown service: {service}", available=registry.names())

    try:
        tools = await pool.list_tools(service)
    except Exception as e:
        return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")

    if not path:
        # OPTIONS / → all tools
        tool_list = []
        for t in tools:
            tool_info: dict = {"name": t.name, "description": t.description or ""}
            if t.inputSchema:
                tool_info["inputSchema"] = t.inputSchema
            # Add scopes_required per method
            tool_info["scopes_required"] = {
                m: scope_required(service, m, t.name)
                for m in ("GET", "POST", "PUT", "PATCH")
            }
            tool_list.append(tool_info)
        return JSONResponse({"service": service, "tools": tool_list})
    else:
        # OPTIONS /{tool}
        tool_name = _resolve_tool_name(path, [t.name for t in tools])
        if tool_name is None:
            tools_available = [t.name for t in tools]
            return _error(404, "tool_not_found", f"Tool not found: {path}", tool=path, available=tools_available)
        tool = next(t for t in tools if t.name == tool_name)
        info: dict = {"name": tool.name, "description": tool.description or ""}
        if tool.inputSchema:
            info["inputSchema"] = tool.inputSchema
        info["scopes_required"] = {
            m: scope_required(service, m, tool.name)
            for m in ("GET", "POST", "PUT", "PATCH")
        }
        return JSONResponse(info)


async def handle_service_get(request: Request, service: str, pool: ConnectionPool, registry: ServiceRegistry) -> Response:
    """GET / → tool names + descriptions; GET /{tool}?params → call_tool (read-only)."""
    path = request.path_params.get("path", "").strip("/")
    entry = registry.get(service)
    if entry is None:
        return _error(404, "unknown_service", f"Unknown service: {service}", available=registry.names())

    if not path:
        # GET / → list tools
        try:
            tools = await pool.list_tools(service)
            return JSONResponse({
                "service": service,
                "tools": [{"name": t.name, "description": t.description or ""} for t in tools],
            })
        except Exception as e:
            return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")

    # GET /{tool}?params → call_tool
    if not authorize(LOCAL_ALLOW_ALL, "GET", f"{service}.localhost", path):
        return _error(403, "forbidden", "Not authorized")

    try:
        tools = await pool.list_tools(service)
    except Exception as e:
        return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")

    tool_name = _resolve_tool_name(path, [t.name for t in tools])
    if tool_name is None:
        return _error(404, "tool_not_found", f"Tool not found: {path}", tool=path, available=[t.name for t in tools])

    args = _merge_arguments(request, b"")
    try:
        result = await pool.call_tool(service, tool_name, args)
        return serialize_tool_result(result)
    except asyncio.TimeoutError:
        return _error(504, "tool_timeout", f"Tool call timed out", tool=tool_name, timeout_seconds=DEFAULT_TIMEOUT)
    except (ConnectionError, OSError) as e:
        return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")


async def handle_service_mutate(request: Request, service: str, pool: ConnectionPool, registry: ServiceRegistry) -> Response:
    """POST/PUT/PATCH /{tool} → call_tool with definer validation."""
    path = request.path_params.get("path", "").strip("/")
    method = request.method.upper()
    entry = registry.get(service)
    if entry is None:
        return _error(404, "unknown_service", f"Unknown service: {service}", available=registry.names())

    if not path:
        return _error(400, "missing_tool", f"{method} requires a tool path")

    # Permission check
    if not authorize(LOCAL_ALLOW_ALL, method, f"{service}.localhost", path):
        return _error(403, "forbidden", "Not authorized")

    # Read payload
    payload = await _get_payload(request)

    # Definer validation
    definer_header = request.headers.get("X-Gimme-Definer")
    session_key = get_session_key()
    definer_result = validate_definer(method, definer_header, payload, session_key)

    if isinstance(definer_result, DefinerError):
        return _error(definer_result.status_code, definer_result.error_type, definer_result.detail)

    # Resolve tool
    try:
        tools = await pool.list_tools(service)
    except Exception as e:
        return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")

    tool_name = _resolve_tool_name(path, [t.name for t in tools])
    if tool_name is None:
        return _error(404, "tool_not_found", f"Tool not found: {path}", tool=path, available=[t.name for t in tools])

    # Merge args
    args = _merge_arguments(request, payload)

    # Call tool
    try:
        result = await pool.call_tool(service, tool_name, args)
        return serialize_tool_result(result, definer_tier=definer_result.tier, canonical=definer_result.canonical)
    except asyncio.TimeoutError:
        return _error(504, "tool_timeout", f"Tool call timed out", tool=tool_name, timeout_seconds=DEFAULT_TIMEOUT)
    except (ConnectionError, OSError) as e:
        return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")


# ── Index handlers (on bare localhost:7001) ──


async def handle_index(request: Request, registry: ServiceRegistry, pool: ConnectionPool) -> Response:
    """GET localhost:7001/ → list all registered services."""
    services = []
    for name, entry in registry.services.items():
        services.append({
            "name": name,
            "url": f"{name}.localhost:7001",
            "transport": entry.transport_type,
            "connected": name in pool.connected_services(),
        })
    return JSONResponse({"services": services})
