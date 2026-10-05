"""HTTP method dispatch under per-method profiles.

HEAD / OPTIONS discover (never invoke). GET / POST / PUT / PATCH / DELETE invoke a tool,
but only through a method the tool's *contract* admits, and only after the request
carries everything the method + contract + tier require at the service's level.

Spec: docs/http-methods/method-profiles.md
"""

from __future__ import annotations

import asyncio
import logging

from mcp.shared.exceptions import McpError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import approval as approval_mod
from . import audit
from . import guard as guard_mod
from . import idempotency as idem
from .config import ServiceRegistry, get_session_key
from .definer import DefinerError, validate_definer
from .guard import canonical_query, loads_strict, validate_clearance_token
from .methods import (
    SAFE_METHODS,
    ContractPins,
    ToolContract,
    admissible_methods,
    allow_header,
    describe,
    effective_contract,
    requirements,
)
from .permissions import LOCAL_ALLOW_ALL, allowed_methods, authorize, scope_required
from .pool import ConnectionPool, DEFAULT_TIMEOUT
from .serializers import serialize_tool_result

logger = logging.getLogger("webspec.handlers")

# Strictest-contract-seen per (service, tool); see methods.ContractPins.
contract_pins = ContractPins()


def _error(status: int, error_type: str, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
    body = {"error": error_type, "detail": detail, **extra}
    return JSONResponse(body, status_code=status, headers=headers)


def _resolve_tool_name(path: str, tool_names: list[str]) -> str | None:
    """Resolve path to tool name. Try exact match first, then slash-to-underscore fallback."""
    if path in tool_names:
        return path
    # Slash-to-underscore fallback (F10)
    alt = path.replace("/", "_")
    if alt in tool_names:
        return alt
    return None


def _resolve_tool(path: str, tools: list):
    name = _resolve_tool_name(path, [t.name for t in tools])
    return None if name is None else next(t for t in tools if t.name == name)


class ArgumentError(ValueError):
    """Request arguments that must be refused rather than guessed at."""


def _schema_types(prop) -> set[str]:
    if not isinstance(prop, dict):
        return set()
    t = prop.get("type")
    types = {t} if isinstance(t, str) else set(t) if isinstance(t, list) else set()
    for branch in ("anyOf", "oneOf"):
        for alt in prop.get(branch) or []:
            types |= _schema_types(alt)
    return types


def _query_arguments(request: Request, input_schema) -> dict:
    """Query parameters as tool arguments, typed by the tool's own inputSchema.

    A parameter whose schema type cannot be a string (integer, number, boolean, array,
    object) is decoded as strict JSON — ``?limit=5&ids=["a","b"]`` — so read-only tools
    with typed arguments remain callable through GET. Anything else stays a string.
    """
    props = (input_schema or {}).get("properties") or {}
    args: dict = {}
    for key, value in request.query_params.items():
        types = _schema_types(props.get(key))
        if not types or "string" in types:
            args[key] = value
            continue
        try:
            args[key] = loads_strict(value)
        except ValueError:
            raise ArgumentError(f"query parameter {key!r} must be JSON of type {sorted(types)}")
    return args


def _body_arguments(body: bytes) -> dict:
    """A non-empty body MUST be one strict-JSON object — never silently dropped."""
    if not body:
        return {}
    try:
        parsed = loads_strict(body)
    except (ValueError, UnicodeDecodeError):
        raise ArgumentError("request body must be a JSON object (strict JSON: no NaN/Infinity)")
    if not isinstance(parsed, dict):
        raise ArgumentError("request body must be a JSON object")
    return parsed


def _arguments(request: Request, body: bytes, input_schema) -> dict:
    query_args = _query_arguments(request, input_schema)
    body_args = _body_arguments(body)
    clash = sorted(set(query_args) & set(body_args))
    if clash:
        raise ArgumentError(f"argument(s) {clash} given in both query and body")
    return {**query_args, **body_args}


def _contract_for(entry, tool) -> ToolContract:
    return effective_contract(contract_pins, entry.name, tool, entry.tools)


def _tool_info(entry, tool, contract: ToolContract) -> dict:
    info: dict = {"name": tool.name, "description": tool.description or ""}
    if tool.inputSchema:
        info["inputSchema"] = tool.inputSchema
    info.update(describe(contract, entry.level))
    info["scopes_required"] = {m: scope_required(entry.name, m, tool.name) for m in info["methods"]}
    return info


def _decorate(response: Response, contract: ToolContract, level: int) -> Response:
    """Response obligations for every tool invocation (spec § Response obligations)."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-WebSpec-Level"] = str(level)
    response.headers["X-WebSpec-Tier"] = contract.tier
    if contract.open_world:
        # Output came from (or touched) the open world: the harness MUST UFO-tag it.
        response.headers["X-UFO-Taint"] = "open-world"
    return response


def _idempotency_response(decision: idem.Decision) -> Response:
    if decision.kind == "replay" and decision.response is not None:
        stored = decision.response
        if stored.body is None:
            return _error(409, "idempotency_result_not_replayable",
                          "A request with this Idempotency-Key already completed; its result was too "
                          "large to keep for replay.", completed_status=stored.status_code)
        headers = {**stored.headers, "Idempotent-Replayed": "true", "Cache-Control": "no-store"}
        return Response(content=stored.body, status_code=stored.status_code,
                        media_type=stored.media_type, headers=headers)
    if decision.kind == "mismatch":
        return _error(422, "idempotency_key_reused",
                      "This Idempotency-Key was already used for a different request.")
    if decision.kind == "in_flight":
        return _error(409, "idempotency_key_in_flight",
                      "A request with this Idempotency-Key is still being processed.")
    if decision.kind == "full":
        return _error(503, "idempotency_store_full",
                      "Too many unsettled Idempotency-Keys; retry later.", headers={"Retry-After": "5"})
    return _error(409, "idempotency_outcome_unknown",
                  "A previous attempt with this Idempotency-Key timed out or failed after reaching the "
                  "tool, so the tool may have run. Verify the outcome, then retry with a new key.")


def _audited_idempotency_response(audit_ctx, decision: idem.Decision) -> Response:
    response = _idempotency_response(decision)
    reason = None if decision.kind == "replay" else f"idempotency_{decision.kind}"
    audit.record(audit_ctx, outcome=f"idempotency:{decision.kind}", status=response.status_code, reason=reason)
    return response


# ── Service-level handlers (on {service}.<domain>) ──


async def handle_service_head(request: Request, service: str, pool: ConnectionPool, registry: ServiceRegistry) -> Response:
    """HEAD / → ping; HEAD /{tool} → tool existence + Allow (its admissible methods)."""
    path = request.path_params.get("path", "").strip("/")
    entry = registry.get(service)
    if entry is None:
        return _error(404, "unknown_service", f"Unknown service: {service}", available=registry.names())

    headers = {"X-WebSpec-Service": service, "X-WebSpec-Level": str(entry.level)}

    if not path:
        # HEAD / → ping + tool count
        try:
            tools = await pool.list_tools(service)
            healthy = await pool.ping(service)
            headers["X-WebSpec-Tool-Count"] = str(len(tools))
            headers["X-WebSpec-Status"] = "connected" if healthy else "degraded"
            methods = allowed_methods(LOCAL_ALLOW_ALL, f"{service}.localhost", "")
            headers["X-Gimme-Allowed"] = ", ".join(methods)
            return Response(status_code=200, headers=headers)
        except Exception as e:
            logger.warning("HEAD %s failed: %s", service, e)
            return Response(status_code=503, headers={"Retry-After": "1", **headers})

    # HEAD /{tool} → tool existence check
    try:
        tools = await pool.list_tools(service)
    except Exception as e:
        logger.warning("HEAD %s/%s failed: %s", service, path, e)
        return Response(status_code=503, headers={"Retry-After": "1", **headers})
    tool = _resolve_tool(path, tools)
    if tool is None:
        return Response(status_code=404, headers=headers)
    contract = _contract_for(entry, tool)
    headers["X-WebSpec-Tool"] = tool.name
    headers["X-WebSpec-Tier"] = contract.tier
    headers["Allow"] = allow_header(contract)
    # Sanitize description for HTTP header (no newlines, control chars)
    desc = (tool.description or "")[:200]
    headers["X-WebSpec-Description"] = " ".join(desc.split())
    return Response(status_code=200, headers=headers)


async def handle_service_options(request: Request, service: str, pool: ConnectionPool, registry: ServiceRegistry) -> Response:
    """OPTIONS / → every tool's contract; OPTIONS /{tool} → one tool's schema, methods, requirements."""
    path = request.path_params.get("path", "").strip("/")
    entry = registry.get(service)
    if entry is None:
        return _error(404, "unknown_service", f"Unknown service: {service}", available=registry.names())

    try:
        tools = await pool.list_tools(service)
    except Exception as e:
        return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")

    if not path:
        return JSONResponse({
            "service": service,
            "level": entry.level,
            "tools": [_tool_info(entry, t, _contract_for(entry, t)) for t in tools],
        })

    tool = _resolve_tool(path, tools)
    if tool is None:
        return _error(404, "tool_not_found", f"Tool not found: {path}", tool=path, available=[t.name for t in tools])
    contract = _contract_for(entry, tool)
    return JSONResponse(_tool_info(entry, tool, contract), headers={"Allow": allow_header(contract)})


async def handle_service_list(request: Request, service: str, pool: ConnectionPool, registry: ServiceRegistry) -> Response:
    """GET / → tool names, descriptions, and the methods each admits."""
    entry = registry.get(service)
    if entry is None:
        return _error(404, "unknown_service", f"Unknown service: {service}", available=registry.names())
    try:
        tools = await pool.list_tools(service)
    except Exception as e:
        return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")
    return JSONResponse({
        "service": service,
        "level": entry.level,
        "tools": [
            {"name": t.name, "description": t.description or "",
             "methods": sorted(admissible_methods(_contract_for(entry, t)))}
            for t in tools
        ],
    })


async def handle_service_invoke(request: Request, service: str, pool: ConnectionPool, registry: ServiceRegistry) -> Response:
    """GET/POST/PUT/PATCH/DELETE /{tool} → call_tool, under the tool's method profile."""
    path = request.path_params.get("path", "").strip("/")
    method = request.method.upper()

    def early(status: int, error_type: str, detail: str, **extra) -> JSONResponse:
        audit.record_request(request, service=service, outcome="denied", status=status, reason=error_type)
        return _error(status, error_type, detail, **extra)

    entry = registry.get(service)
    if entry is None:
        return early(404, "unknown_service", f"Unknown service: {service}", available=registry.names())
    if not path:
        return early(400, "missing_tool", f"{method} requires a tool path")
    if not authorize(LOCAL_ALLOW_ALL, method, f"{service}.localhost", path):
        return early(403, "forbidden", "Not authorized")
    try:
        tools = await pool.list_tools(service)
    except Exception as e:
        return early(503, "service_unavailable", f"Could not connect to {service}: {e}")
    tool = _resolve_tool(path, tools)
    if tool is None:
        return early(404, "tool_not_found", f"Tool not found: {path}", tool=path, available=[t.name for t in tools])
    tool_name = tool.name

    contract = _contract_for(entry, tool)
    level = entry.level
    body = await request.body()
    host = request.headers.get("host", "")
    cquery = canonical_query(request.url.query)
    audit_ctx = audit.Context(service=service, host=host, method=method, path="/" + path, tool=tool_name,
                              level=level, contract=contract, query=cquery, body=body)

    def deny(status: int, error_type: str, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
        audit.record(audit_ctx, outcome="denied", status=status, reason=error_type)
        return _error(status, error_type, detail, headers=headers, **extra)

    # 1. Method binding: the gateway, not the caller, decides how a tool may be reached.
    admissible = admissible_methods(contract)
    if method not in admissible:
        return deny(
            405, "method_not_allowed",
            f"{method} cannot invoke {tool_name}; its contract admits {', '.join(sorted(admissible))}",
            headers={"Allow": allow_header(contract)},
            tool=tool_name, allowed=sorted(admissible), contract=contract.as_dict(),
        )
    reqs = requirements(method, contract, level)

    # 2. Method rules
    if reqs.empty_body and body:
        return deny(400, "body_not_allowed", "GET carries arguments in the query string only; the body must be empty")

    definer_tier, canonical_definer = 0, ""
    if reqs.definer:
        result = validate_definer(method, request.headers.get("X-Gimme-Definer"), body, get_session_key())
        if isinstance(result, DefinerError):
            return deny(result.status_code, result.error_type, result.detail)
        if reqs.bookend and result.tier < 2:
            return deny(403, "bookend_required",
                        f"Level {level} requires a Tier-2 bookend: X-Gimme-Definer: {result.canonical}:<bookend>")
        definer_tier, canonical_definer = result.tier, result.canonical

    try:
        args = _arguments(request, body, tool.inputSchema)
    except ArgumentError as e:
        return deny(400, "invalid_arguments", str(e))

    # 3. Idempotency: replay before re-checking one-shot credentials (clearance, approval).
    idem_key = request.headers.get("Idempotency-Key")
    use_idem = method not in SAFE_METHODS and (reqs.idempotency_key or idem_key is not None)
    fingerprint = ""
    if use_idem:
        if not idem_key:
            return deny(400, "idempotency_key_required",
                        f"Level {level} requires an Idempotency-Key header for non-idempotent tool {tool_name}")
        if not idem.valid_key(idem_key):
            return deny(400, "idempotency_key_invalid", "Idempotency-Key must be 1-255 visible ASCII characters")
        fingerprint = idem.request_fingerprint(method, "/" + path, cquery, body)
        early_decision = idem.store.peek(service, idem_key, fingerprint)
        if early_decision.kind != "proceed":
            return _audited_idempotency_response(audit_ctx, early_decision)

    # 4. Contract + tier rules (clearance is checked here, spent only at commit — step 5)
    clearance_header = request.headers.get("X-UFO-Clearance")
    if reqs.clearance:
        err = validate_clearance_token(get_session_key(), tool_name, args, clearance_header,
                                       service=service, method=method, spend=False)
        if err:
            return deny(403, err, f"Level {level} requires a valid, unspent X-UFO-Clearance token for "
                                  f"{method} {tool_name} on {service}")

    if reqs.approval:
        summary = approval_mod.request_summary(method, service, host, "/" + path, tool_name, args, body)
        approval_header = request.headers.get("X-WebSpec-Approval")
        if not approval_header:
            challenge = approval_mod.store.issue(summary)
            if challenge is None:
                return deny(429, "approval_queue_full", "Too many pending approval challenges; retry later",
                            headers={"Retry-After": "30"})
            audit.record(audit_ctx, outcome="approval_challenged", status=428, reason="approval_required")
            return JSONResponse(challenge, status_code=428)
        err = await approval_mod.store.verify(approval_header, approval_mod.summary_fingerprint(summary))
        if err:
            status = 503 if err == "approval_unavailable" else 403
            return deny(status, err, "Human approval missing, invalid, expired, reused, or for a different request")

    # 5. Commit: check → claim → spend, with no await from here until call_tool starts, so
    #    nothing can be double-spent and nothing one-shot is spent unless the call runs.
    if reqs.clearance and guard_mod.clearance_spent(clearance_header, service):
        return deny(403, "clearance_reused", "This X-UFO-Clearance token was already spent")
    if reqs.approval and not approval_mod.store.can_spend(approval_header):
        return deny(403, "approval_reused", "This approval was already spent")
    if use_idem:
        claim = idem.store.begin(service, idem_key, fingerprint)
        if claim.kind != "proceed":
            return _audited_idempotency_response(audit_ctx, claim)
    if reqs.clearance:
        guard_mod.spend_clearance(clearance_header, service)
    if reqs.approval:
        approval_mod.store.spend(approval_header)

    # 6. Invoke. From here on the tool may have run, so every exit settles the key.
    try:
        result = await pool.call_tool(service, tool_name, args)
    except McpError as e:
        # A JSON-RPC error *response*: the server refused the call, so it did not run.
        if use_idem:
            idem.store.abandon(service, idem_key)
        audit.record(audit_ctx, outcome="rejected", status=502, reason="tool_rejected")
        return _error(502, "tool_rejected", f"{service} rejected the call: {e}", tool=tool_name)
    except asyncio.TimeoutError:
        if use_idem:
            idem.store.mark_unknown(service, idem_key)
        audit.record(audit_ctx, outcome="timeout", status=504, reason="tool_timeout")
        return _error(504, "tool_timeout", "Tool call timed out", tool=tool_name, timeout_seconds=DEFAULT_TIMEOUT)
    except BaseException as e:
        if use_idem:
            idem.store.mark_unknown(service, idem_key)
        if isinstance(e, (ConnectionError, OSError)):
            audit.record(audit_ctx, outcome="error", status=503, reason="service_unavailable")
            return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")
        audit.record(audit_ctx, outcome="error", status=500, reason="internal_error")
        raise

    try:
        response = _decorate(serialize_tool_result(result, definer_tier=definer_tier, canonical=canonical_definer),
                             contract, level)
        if use_idem:
            idem.store.complete(service, idem_key,
                                idem.snapshot_response(response.status_code, response.body,
                                                       response.media_type, response.headers))
    except Exception:
        logger.exception("Tool %s/%s ran but its result could not be returned", service, tool_name)
        if use_idem:
            idem.store.mark_unknown(service, idem_key)
        audit.record(audit_ctx, outcome="invoked", status=500, reason="result_unserializable")
        return _error(500, "result_unserializable", "The tool ran, but its result could not be returned.",
                      tool=tool_name)
    audit.record(audit_ctx, outcome="invoked", status=response.status_code,
                 reason="tool_error" if result.isError else None, definer=canonical_definer or None)
    return response


# ── Index handlers (on bare localhost:7001) ──


async def handle_index(request: Request, registry: ServiceRegistry, pool: ConnectionPool) -> Response:
    """GET localhost:7001/ → list all registered services."""
    services = []
    for name, entry in registry.services.items():
        services.append({
            "name": name,
            "url": f"{name}.localhost:7001",
            "transport": entry.transport_type,
            "level": entry.level,
            "connected": name in pool.connected_services(),
        })
    return JSONResponse({"services": services})
