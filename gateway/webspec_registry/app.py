from __future__ import annotations

from dataclasses import asdict

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from .graph import build_graph, to_dot
from .resolver import resolve


def create_registry_app(catalog_fn, accounts_fn=lambda: []) -> Starlette:
    """Build the registry service. catalog_fn() -> list[ToolRecord]; accounts_fn() -> list[AccountRecord].

    NOTE (tier B): this app is localhost-only. Exposing it publicly requires porting the gateway
    guard middleware — TODO(C).
    """
    async def resolve_ep(request):
        q = request.query_params.get("q", "")
        results = resolve(q, catalog_fn())
        return JSONResponse({"query": q, "results": [
            {"service": r.record.service, "tool": r.record.tool, "verb": r.record.verb,
             "noun": r.record.noun, "tier": r.record.tier, "status": r.status,
             "score": round(r.score, 3)} for r in results]})

    async def catalog_ep(request):
        return JSONResponse({"tools": [asdict(r) for r in catalog_fn()]})

    async def graph_ep(request):
        g = build_graph(catalog_fn(), accounts_fn())
        if request.query_params.get("format") == "dot":
            return PlainTextResponse(to_dot(g))
        return JSONResponse({"nodes": g.nodes, "edges": g.edges})

    return Starlette(routes=[
        Route("/resolve", resolve_ep, methods=["GET"]),
        Route("/catalog", catalog_ep, methods=["GET"]),
        Route("/graph", graph_ep, methods=["GET"]),
    ])
