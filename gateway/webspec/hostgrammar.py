"""Host grammar: ``{qualifier}*.{destination}.{domain}``.

- The **destination** — the label immediately left of the domain — is the only label
  that routes to a service and the only isolation boundary (audience, guard, contract).
- **Qualifiers** further left are optional routing selectors (region, version,
  environment…). Each must appear in the destination's own allow-list (config key
  ``"labels"``), each at most once, in the allow-list's order. The default allow-list is
  empty, so an unconfigured service accepts exactly ``{destination}.{domain}``.
- Labels never carry identity or meaning: identity lives in signed credentials, meaning
  in the path. (Label hierarchy is a cookie trust hierarchy and wildcard certificates
  for deeper labels are public in CT logs — so labels must be safe to be public and
  safe to share a trust zone with their destination.)

Spec: docs/http-methods/method-profiles.md § Host grammar
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_QUALIFIERS = 4


@dataclass(frozen=True)
class HostMatch:
    destination: str
    qualifiers: tuple[str, ...]


def split_labels(captured: str) -> tuple[str, tuple[str, ...]] | None:
    """Split the routed prefix (everything left of the domain) into (destination, qualifiers).

    Returns None if any label is not a canonical lowercase DNS label.
    """
    labels = captured.split(".")
    if not labels or any(not _LABEL.fullmatch(lbl) for lbl in labels):
        return None
    return labels[-1], tuple(labels[:-1])


def qualifiers_allowed(qualifiers: Sequence[str], allow_list: Sequence[str] | None) -> bool:
    """True iff every qualifier is allow-listed, none repeats, and they follow allow-list order."""
    if not qualifiers:
        return True
    if not allow_list or len(qualifiers) > MAX_QUALIFIERS:
        return False
    positions = []
    for q in qualifiers:
        if q not in allow_list:
            return False
        positions.append(list(allow_list).index(q))
    return positions == sorted(set(positions)) and len(positions) == len(set(positions))
