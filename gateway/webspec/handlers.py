"""HTTP method dispatch under per-method profiles.

HEAD / OPTIONS discover (never invoke). Every other method resolves a tool through the
destination's route map (routes.py: the path, the method, and on POST the definer), then
invokes it, but only through a method the tool's *contract* admits, and only after the request
carries everything the method + contract + tier require at the service's level.

Spec: docs/spec/addressing.md (route map), docs/spec/methods.md (binding, contracts) and
docs/spec/levels.md (requirements)
"""

from __future__ import annotations

import asyncio
import logging
import os

import anyio
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from . import approval as approval_mod
from . import audit
from . import guard as guard_mod
from . import idempotency as idem
from .config import GuardKeyError, ServiceEntry, ServiceRegistry, get_session_key, is_public_host
from .definer import DefinerError, DefinerResult, definer_header, validate_definer
from .guard import canonical_query, loads_strict, validate_clearance_token
from .methods import (
    INVOKING_METHODS,
    SAFE_METHODS,
    ContractPins,
    ToolContract,
    admissible_methods,
    effective_contract,
    overridden,
    requirements,
)
from .permissions import LOCAL_ALLOW_ALL, authorize
from .pool import ConnectionPool, DEFAULT_TIMEOUT
from .routes import BindingEvent, Refusal, RouteMap, key_of
from .serializers import serialize_tool_result

logger = logging.getLogger("webspec.handlers")

# Strictest-contract-seen per (service, tool); see methods.ContractPins.
contract_pins = ContractPins()
# One route map per destination (routes.RouteMap), holding its binding pins and the tools its
# first build moved. Like contract pins, they live for the process and survive a destination
# being removed from the config and added back (PA-8): re-adding it is a rebuild.
route_maps: dict[str, RouteMap] = {}


def _error(status: int, error_type: str, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
    body = {"error": error_type, "detail": detail, **extra}
    return JSONResponse(body, status_code=status, headers=headers)


GUARD_KEY_UNAVAILABLE = "guard_key_unavailable"


def refuse_without_guard_key(request: Request, service: str, exc: GuardKeyError,
                             ctx: audit.Context | None = None) -> Response:
    """GD-5: while the guard key is unavailable, a request that needs it gets a plain-text 500.

    AU-1: the refusal is audited, with outcome ``error`` (``ctx`` once the tool is resolved).
    The log gets one line per refusal and no traceback. It never holds the key: a
    GuardKeyError names the variable or the file, never their content.
    """
    if ctx is None:
        audit.record_request(request, service=service, outcome="error", status=500, reason=GUARD_KEY_UNAVAILABLE)
    else:
        audit.record(ctx, outcome="error", status=500, reason=GUARD_KEY_UNAVAILABLE)
    logger.error("Refused %s on %s with 500, the guard key is unavailable (GD-5): %s", request.method.upper(),
                 service, exc)
    return PlainTextResponse("Internal Server Error", status_code=500)


def _unknown_service(request: Request, service: str, registry: ServiceRegistry) -> dict:
    """Arguments of a ``404 unknown_service`` (HG-1). The destination list is for local callers.

    On the public domain it is left out: it would name every destination, level-0 ones
    included, which HG-7 keeps off the public domain, to anyone asking for one that is not
    configured.
    """
    extra = {} if is_public_host(request.headers.get("host", "")) else {"available": registry.names()}
    return {"status": 404, "error_type": "unknown_service", "detail": f"Unknown service: {service}", **extra}


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


async def _list_tools(pool: ConnectionPool, entry: ServiceEntry) -> tuple[list, RouteMap]:
    """The destination's tools and its route map, which pins every listed tool's contract (TC-6).

    Pinning only the tool a request names would let a later listing loosen a tool the gateway
    has listed but nothing has yet described or called: a server could then re-list a
    destructive tool as read-only.
    """
    tools = await pool.list_tools(entry.name)
    return tools, _route_map(entry, tools)


def _contract_for(entry, tool) -> ToolContract:
    return effective_contract(contract_pins, entry.name, tool, entry.tools)


def _route_map(entry: ServiceEntry, tools: list) -> RouteMap:
    """The destination's route map, built at its first listing and rebuilt whenever its inputs change.

    Its inputs are the listed tools, their effective contracts and the config's ``bindings``,
    so comparing them rebuilds the map at every re-list that changes anything and at every
    config reload (PA-7). No ``await`` runs between reading the map and storing its successor,
    so a request sees either the old map or the new one.
    """
    contracts = {t.name: _contract_for(entry, t) for t in tools}
    current = route_maps.get(entry.name)
    if current is not None and current.contracts == contracts and current.bindings == entry.bindings:
        return current
    if current is None:
        current = RouteMap.build(entry.name, contracts)  # PA-6; the rebuild applies the bindings
    rebuilt = current.rebuild(contracts, entry.bindings)
    for event in rebuilt.events:
        _audit_binding(entry, rebuilt, event)
    route_maps[entry.name] = rebuilt
    return rebuilt


def _audit_binding(entry: ServiceEntry, route_map: RouteMap, event: BindingEvent) -> None:
    """AU-5: a pinned key that now resolves to another tool, or its confirmation."""
    pinned = contract_pins.get(entry.name, event.pinned_tool)
    pinned = overridden(pinned, entry.name, event.pinned_tool, entry.tools) if pinned else None
    ctx = audit.Context(service=entry.name, host=None, method=event.method, path=event.path, tool=event.tool,
                        level=entry.level, contract=route_map.contracts.get(event.tool))
    audit.record(ctx, outcome=event.kind, status=None, reason=None, definer=event.definer or None,
                 pinned_tool=event.pinned_tool, pinned_tier=pinned.tier if pinned else None,
                 pinned_risk=pinned.risk if pinned else None)


def _entries(entry: ServiceEntry, tools: list, route_map: RouteMap, path: str | None = None) -> list[dict]:
    """DS-1: one entry per key on ``path`` (on every path, without one), or the error of a closed key."""
    schemas = {t.name: t.inputSchema for t in tools}
    rows = []
    for (method, noun, definer), target in route_map.table(path):
        row = {"path": f"/{noun}", "method": method, **({"definer": definer} if method == "POST" else {})}
        if isinstance(target, Refusal):
            row["error"] = target.error
        else:
            contract = route_map.contracts[target]
            row.update(tool=target, inputSchema=schemas.get(target) or {}, contract=contract.as_dict(),
                       requirements=requirements(method, contract, entry.level).as_dict())
        rows.append(row)
    return rows


def _refusal(refuse, r: Refusal) -> JSONResponse:
    """A route refusal (PA-10) through ``refuse``: the path's Allow on a 405, the tools on a closed key."""
    extra = {}
    if r.error == "binding_changed":
        extra = {"pinned_tool": r.tools[0], "tool": r.tools[1]}
    elif r.tools:
        extra = {"tools": list(r.tools)}
    return refuse(r.status, r.error, r.detail, headers={"Allow": r.allow} if r.allow else None, **extra)


def _decorate(response: Response, contract: ToolContract, level: int) -> Response:
    """Response obligations for every tool invocation (spec § Response obligations)."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["WebSpec-Level"] = str(level)
    response.headers["WebSpec-Tier"] = contract.tier
    if contract.open_world:
        # Output came from (or touched) the open world: the harness MUST UFO-tag it (RS-2).
        response.headers["WebSpec-Taint"] = "open-world"
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
    """HEAD / → health; HEAD /{noun} → the path's Allow and a link to the map (DS-2). Never invokes."""
    path = request.path_params.get("path", "").strip("/")
    entry = registry.get(service)
    if entry is None:
        return _error(**_unknown_service(request, service, registry))

    headers = {"WebSpec-Service": service, "WebSpec-Level": str(entry.level)}
    try:
        tools, route_map = await _list_tools(pool, entry)
        if not path:
            healthy = await pool.ping(service)
    except Exception as e:
        logger.warning("HEAD %s/%s failed: %s", service, path, e)
        return Response(status_code=503, headers={"Retry-After": "1", **headers})

    if not path:
        headers["WebSpec-Tool-Count"] = str(len(tools))
        headers["WebSpec-Status"] = "connected" if healthy else "degraded"
        return Response(status_code=200, headers=headers)
    # The route, not whether an item exists: HEAD never reaches the tool.
    allow = route_map.allow(path)
    if allow is None:
        return Response(status_code=404, headers=headers)
    return Response(status_code=200, headers={**headers, "Allow": allow, "Link": '</>; rel="service-desc"'})


async def handle_service_options(request: Request, service: str, pool: ConnectionPool, registry: ServiceRegistry) -> Response:
    """OPTIONS / → the whole route map; OPTIONS /{noun} → the path's entries and Allow (DS-1)."""
    path = request.path_params.get("path", "").strip("/")
    entry = registry.get(service)
    if entry is None:
        return _error(**_unknown_service(request, service, registry))

    try:
        tools, route_map = await _list_tools(pool, entry)
    except Exception as e:
        return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")

    allow = route_map.allow(path) if path else None
    if path and allow is None:
        return _error(404, "tool_not_found", f"/{path} maps no tool")
    body = {"service": service, "level": entry.level, "entries": _entries(entry, tools, route_map, path or None)}
    return JSONResponse(body, headers={"Allow": allow} if allow else None)


async def handle_service_list(request: Request, service: str, pool: ConnectionPool, registry: ServiceRegistry) -> Response:
    """GET / → every tool with its description and its entry: method, path, and on POST the definer (DS-3)."""
    entry = registry.get(service)
    if entry is None:
        return _error(**_unknown_service(request, service, registry))
    try:
        tools, route_map = await _list_tools(pool, entry)
    except Exception as e:
        return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")
    listed = []
    for t in tools:
        method, noun, definer = key = key_of(route_map.entries[t.name])
        row = {"name": t.name, "description": t.description or "", "method": method, "path": f"/{noun}"}
        if method == "POST":
            row["definer"] = definer
        if isinstance(route_map.keys.get(key), Refusal):
            row["error"] = route_map.keys[key].error
        listed.append(row)
    return JSONResponse({"service": service, "level": entry.level, "tools": listed})


async def handle_service_invoke(request: Request, service: str, pool: ConnectionPool, registry: ServiceRegistry) -> Response:
    """Every other method on /{noun}: resolve the tool (PA-10), then call it under its method profile."""
    path = request.path_params.get("path", "").strip("/")
    method = request.method.upper()

    def early(status: int, error_type: str, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
        audit.record_request(request, service=service, outcome="denied", status=status, reason=error_type)
        return _error(status, error_type, detail, headers=headers, **extra)

    entry = registry.get(service)
    if entry is None:
        return early(**_unknown_service(request, service, registry))
    if not path:
        if method in INVOKING_METHODS:  # PA-3; GET / is the listing and never gets here
            return early(400, "missing_tool", f"{method} requires a tool path")
        return early(405, "method_not_allowed", f"/ does not allow {method}", headers={"Allow": "HEAD, OPTIONS, GET"})
    if not authorize(LOCAL_ALLOW_ALL, method, f"{service}.localhost", path):
        return early(403, "forbidden", "Not authorized")
    try:
        tools, route_map = await _list_tools(pool, entry)
    except Exception as e:
        return early(503, "service_unavailable", f"Could not connect to {service}: {e}")

    # PA-10, in order: the path maps nothing (404), not the method (405), then on POST the
    # definer, which names the action (400), then a closed key (503).
    definer = validate_definer(method, definer_header(request.headers))
    verb = definer.canonical if isinstance(definer, DefinerResult) else ""
    target = route_map.resolve(method, "/" + path, verb)
    if isinstance(target, Refusal) and target.status in (404, 405):
        return _refusal(early, target)
    if method == "POST" and isinstance(definer, DefinerError):
        return early(definer.status_code, definer.error_type, definer.detail)
    if isinstance(target, Refusal):
        return _refusal(early, target)
    tool_name = target
    tool = next(t for t in tools if t.name == tool_name)

    contract = route_map.contracts[tool_name]
    level = entry.level
    body = await request.body()
    host = request.headers.get("host", "")
    cquery = canonical_query(request.url.query)
    audit_ctx = audit.Context(service=service, host=host, method=method, path="/" + path, tool=tool_name,
                              level=level, contract=contract, query=cquery, body=body)

    def deny(status: int, error_type: str, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
        audit.record(audit_ctx, outcome="denied", status=status, reason=error_type)
        return _error(status, error_type, detail, headers=headers, **extra)

    # 1. Method binding (MB-2), on every request: the map only narrows it (PA-5).
    if method not in admissible_methods(contract):
        return deny(405, "method_not_allowed", f"{method} cannot invoke {tool_name} under its contract",
                    headers={"Allow": route_map.allow(path)}, tool=tool_name, contract=contract.as_dict())
    reqs = requirements(method, contract, level)

    # 2. Method rules
    if reqs.empty_body and body:
        return deny(400, "body_not_allowed", "GET carries arguments in the query string only; the body must be empty")

    # DF-1, DF-2 on PUT, PATCH and DELETE: optional, checked only when sent (POST's was checked with the route).
    if isinstance(definer, DefinerError):
        return deny(definer.status_code, definer.error_type, definer.detail)
    canonical_definer = definer.canonical

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
        early_decision = idem.store.peek(service, idem_key, fingerprint, tool_name)
        if early_decision.kind != "proceed":
            return _audited_idempotency_response(audit_ctx, early_decision)

    # 4. Contract + tier rules (clearance is checked here, spent only at commit — step 5)
    clearance_header = request.headers.get("WebSpec-Clearance")
    if reqs.clearance:
        try:
            session_key = get_session_key()
        except GuardKeyError as exc:
            return refuse_without_guard_key(request, service, exc, audit_ctx)
        err = validate_clearance_token(session_key, tool_name, args, clearance_header,
                                       service=service, method=method, spend=False)
        if err:
            return deny(403, err, f"Level {level} requires a valid, unspent WebSpec-Clearance token for "
                                  f"{method} {tool_name} on {service}")

    if reqs.approval:
        if not approval_mod.approvers_available():
            return deny(503, "approval_unavailable",
                        f"Level {level} requires human approval, and no approvers are configured")
        summary = approval_mod.request_summary(method, service, host, "/" + path, tool_name, args, body)
        approval_header = request.headers.get("WebSpec-Approval")
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
        return deny(403, "clearance_reused", "This WebSpec-Clearance token was already spent")
    if reqs.approval and not approval_mod.store.can_spend(approval_header):
        return deny(403, "approval_reused", "This approval was already spent")
    if use_idem:
        claim = idem.store.begin(service, idem_key, fingerprint, tool_name)
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
        if e.error.code == CONNECTION_CLOSED:
            # The connection closed mid-call (e.g. the server crashed): the tool may have run.
            if use_idem:
                idem.store.mark_unknown(service, idem_key)
            audit.record(audit_ctx, outcome="error", status=503, reason="connection_closed")
            return _error(503, "service_unavailable",
                          f"{service} closed the connection during the call; the tool may have run",
                          tool=tool_name)
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
        if isinstance(e, (ConnectionError, OSError, anyio.ClosedResourceError, anyio.BrokenResourceError)):
            audit.record(audit_ctx, outcome="error", status=503, reason="service_unavailable")
            return _error(503, "service_unavailable", f"Could not connect to {service}: {e}")
        audit.record(audit_ctx, outcome="error", status=500, reason="internal_error")
        raise

    try:
        response = _decorate(serialize_tool_result(result, canonical=canonical_definer), contract, level)
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
    """GET localhost:{port}/ → list all registered services."""
    port = os.environ.get("WEBSPEC_PORT", "7001")
    services = []
    for name, entry in registry.services.items():
        services.append({
            "name": name,
            "url": f"{name}.localhost:{port}",
            "transport": entry.transport_type,
            "level": entry.level,
            "connected": name in pool.connected_services(),
        })
    return JSONResponse({"services": services})
