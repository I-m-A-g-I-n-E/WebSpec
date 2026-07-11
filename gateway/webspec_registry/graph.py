from __future__ import annotations

from dataclasses import dataclass

TIER_RANK = {"open": 0, "sensitive": 1, "dangerous": 2}


@dataclass
class Graph:
    nodes: list[dict]  # {id, type, label}
    edges: list[dict]  # {src, dst, rel}


def build_graph(catalog, accounts=()) -> Graph:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add(nid: str, ntype: str, label: str | None = None) -> None:
        nodes.setdefault(nid, {"id": nid, "type": ntype, "label": label or nid})

    for rec in catalog:
        s, v, n, t = (f"service:{rec.service}", f"verb:{rec.verb}",
                      f"noun:{rec.noun}", f"tier:{rec.tier}")
        add(s, "service"); add(v, "verb"); add(n, "noun"); add(t, "tier")
        edges.append({"src": s, "dst": n, "rel": "exposes"})
        edges.append({"src": n, "dst": v, "rel": rec.tool})
        edges.append({"src": v, "dst": t, "rel": "tier"})

    for acc in accounts:
        aid = f"account:{acc.vault}/{acc.item}"
        add(aid, "account", acc.title)
        if acc.linked_service:
            add(f"service:{acc.linked_service}", "service")
            edges.append({"src": aid, "dst": f"service:{acc.linked_service}", "rel": "credentials"})

    return Graph(nodes=list(nodes.values()), edges=edges)


def _esc(s: str) -> str:
    """Escape backslashes and quotes for DOT format."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def to_dot(graph: Graph) -> str:
    lines = ["digraph webspec {"]
    for nd in graph.nodes:
        esc_id = _esc(nd["id"])
        esc_label = _esc(nd["label"])
        lines.append(f'  "{esc_id}" [label="{esc_label}"];')
    for e in graph.edges:
        esc_src = _esc(e["src"])
        esc_dst = _esc(e["dst"])
        esc_rel = _esc(e["rel"])
        lines.append(f'  "{esc_src}" -> "{esc_dst}" [label="{esc_rel}"];')
    lines.append("}")
    return "\n".join(lines)


def poset(catalog) -> dict[tuple[str, str], int]:
    """(verb, noun) → highest tier rank observed. The ordering relation is open ⊑ sensitive ⊑ dangerous."""
    out: dict[tuple[str, str], int] = {}
    for rec in catalog:
        key = (rec.verb, rec.noun)
        out[key] = max(out.get(key, 0), TIER_RANK[rec.tier])
    return out

# TODO(C): noun-containment ordering (message ⊑ channel ⊑ workspace) for a richer poset —
# requires the noun hierarchy sketched in docs/url-grammar/object-type-system.md.
