from __future__ import annotations

from dataclasses import dataclass

from .matcher import LexicalMatcher, Matcher
from .records import ToolRecord

STATUS_WEIGHT = {"connected": 1.0, "keychain": 0.8, "available": 0.5, "unavailable": 0.0}


@dataclass
class Ranked:
    record: ToolRecord
    score: float
    status: str


def resolve(query, catalog, status_of=None, matcher: Matcher | None = None, top_k: int = 10):
    """Rank catalog tools for a query using matcher score × status weight (the three-way join)."""
    matcher = matcher or LexicalMatcher()
    status_of = status_of or (lambda r: "available")
    ranked: list[Ranked] = []
    for rec in catalog:
        raw = matcher.score(query, rec)
        if raw <= 0.0:
            continue
        status = status_of(rec)
        final = raw * STATUS_WEIGHT.get(status, 0.0)
        if final <= 0.0:
            continue
        ranked.append(Ranked(record=rec, score=final, status=status))
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked[:top_k]

# TODO(C): recency_boost, preference_boost, per-user history — see docs/discovery/three-way-join.md.
