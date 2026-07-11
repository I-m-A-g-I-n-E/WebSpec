from __future__ import annotations

import os
import re
from pathlib import Path

# Canonical verb map (seeded from docs/discovery predicate tables).
VERB_CANON = {
    "send": "send", "post": "send", "notify": "send", "ping": "send", "dm": "send",
    "fire": "send", "shoot": "send", "drop": "send", "email": "send",
    "read": "read", "get": "read", "list": "read", "fetch": "read", "search": "read",
    "grab": "read", "pull": "read", "check": "read", "view": "read",
    "create": "create", "add": "create", "new": "create",
    "delete": "delete", "remove": "delete", "nuke": "delete", "trash": "delete",
    "update": "modify", "modify": "modify", "patch": "modify", "edit": "modify",
    "set": "modify", "tweak": "modify", "fix": "modify", "rename": "modify",
    "run": "invoke", "invoke": "invoke", "execute": "invoke", "exec": "invoke",
}

NOUN_CANON = {
    "email": "message", "message": "message", "note": "message", "dm": "message",
    "msg": "message", "mail": "message", "notification": "message",
    "file": "file", "document": "document", "doc": "document", "page": "document",
    "secret": "secret", "item": "secret", "password": "secret", "credential": "secret",
    "vault": "vault",
}

def _find_atlas_path() -> Path | None:
    """Resolve the Verb Atlas YAML path, or None if it can't be found.

    Priority: WEBSPEC_VERB_ATLAS env var if set (used exclusively -- if it
    points nowhere, that's a definitive "not found", no fallback); otherwise
    the repo-root-relative default, then a cwd-relative default.
    """
    env_path = os.environ.get("WEBSPEC_VERB_ATLAS")
    if env_path:
        p = Path(env_path)
        return p if p.exists() else None

    for candidate in (
        # gateway/webspec_registry/normalize.py -> repo root is parents[2]
        Path(__file__).resolve().parents[2] / "docs" / "http-methods" / "verb-atlas.yaml",
        Path.cwd() / "docs" / "http-methods" / "verb-atlas.yaml",
    ):
        if candidate.exists():
            return candidate
    return None


def _load_atlas_verb_map() -> dict[str, str]:
    """Load an optional verb -> canonical map from the Verb Atlas.

    The atlas (docs/http-methods/verb-atlas.yaml) is a curated 302-verb
    vocabulary organized as families -> canonical verbs -> rotation
    candidates. This enrichment is strictly optional: normalize() must work
    with only the stdlib and the hand-seeded VERB_CANON. Any reason the
    atlas can't be loaded (PyYAML missing, file missing, malformed YAML)
    results in an empty dict -- never a crash.
    """
    try:
        import yaml
    except ImportError:
        return {}

    path = _find_atlas_path()
    if path is None:
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        result: dict[str, str] = {}
        for family in data["families"].values():
            for canonical, spec in family["verbs"].items():
                canon_lower = canonical.lower()
                result[canon_lower] = canon_lower
                for rotation in spec.get("rotation_candidates") or []:
                    result[rotation.lower()] = canon_lower
        return result
    except Exception:
        return {}


# TODO(C): bundle verb-atlas.yaml as package data so enrichment works in a minimal container that doesn't ship docs/.
# Merged lookup used by normalize(): atlas entries first, seed VERB_CANON
# last so it always wins on conflict. Built once at import time.
_EFFECTIVE_VERB_CANON = {**_load_atlas_verb_map(), **VERB_CANON}

_READ_VERBS = {"read"}

_OPAUTH_TIERS = {
    "list_vaults": "open", "read": "sensitive", "list_items": "sensitive",
    "get_item": "sensitive", "run": "dangerous",
}


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[_\W]+", s.lower()) if t]


def _canon_noun(tok: str) -> str:
    if tok in NOUN_CANON:
        return NOUN_CANON[tok]
    if tok.endswith("s") and tok[:-1] in NOUN_CANON:  # light singularization (vaults→vault)
        return NOUN_CANON[tok[:-1]]
    return tok


def normalize(tool: str, description: str = "") -> tuple[str, str]:
    """Return (canonical_verb, canonical_noun) for a tool name or free-text query."""
    parts = _tokens(tool)
    verb = ""
    for p in parts:                      # first token that maps to a verb
        if p in _EFFECTIVE_VERB_CANON:
            verb = _EFFECTIVE_VERB_CANON[p]
            break
    if not verb and parts:
        verb = _EFFECTIVE_VERB_CANON.get(parts[0], parts[0])

    noun = ""
    for p in reversed(parts):            # last token that maps to a noun
        c = _canon_noun(p)
        if p in NOUN_CANON or (p.endswith("s") and p[:-1] in NOUN_CANON):
            noun = c
            break
    if not noun and parts:
        noun = _canon_noun(parts[-1])
    return verb, noun


def tier_of(service: str, tool: str, verb: str) -> str:
    """Sensitivity tier. op-auth uses its UFO tiers; others default by verb."""
    if service == "op-auth":
        return _OPAUTH_TIERS.get(tool, "dangerous")
    return "open" if verb in _READ_VERBS else "sensitive"
