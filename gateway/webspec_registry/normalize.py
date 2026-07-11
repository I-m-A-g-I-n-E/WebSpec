from __future__ import annotations

import re

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
        if p in VERB_CANON:
            verb = VERB_CANON[p]
            break
    if not verb and parts:
        verb = VERB_CANON.get(parts[0], parts[0])

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
