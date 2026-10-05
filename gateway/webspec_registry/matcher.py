from __future__ import annotations

import re
from abc import ABC, abstractmethod

from .normalize import normalize
from .records import ToolRecord


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[_\W]+", s.lower()) if t}


class Matcher(ABC):
    """Scores how well a natural-language query matches a tool. Range [0.0, 1.0]."""

    @abstractmethod
    def score(self, query: str, record: ToolRecord) -> float: ...


class LexicalMatcher(Matcher):
    """Zero-dependency matcher: token overlap + canonical verb/noun bonus."""

    def score(self, query: str, record: ToolRecord) -> float:
        q = _tokens(query)
        if not q:
            return 0.0
        doc = _tokens(record.tool) | _tokens(record.description) | {record.verb, record.noun}
        overlap = len(q & doc) / len(q)
        qverb, qnoun = normalize(query)
        bonus = 0.0
        if qverb and qverb == record.verb:
            bonus += 0.25
        if qnoun and qnoun == record.noun:
            bonus += 0.25
        return min(1.0, overlap + bonus)

# TODO(C): EmbeddingMatcher — local small model or API backend, multi-vector
# (canonical/predicate/object) search. See docs/discovery/embedding-schema.md.
