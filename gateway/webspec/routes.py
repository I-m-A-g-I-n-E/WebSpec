"""The route rule (PA-4) and a destination's route map (PA-6 to PA-10).

The path is a noun and the method is the verb; on POST the definer names the action.
``route()`` gives one tool its entry from its name and its contract alone. A ``RouteMap``
holds one destination's entries and the binding pins that keep a renamed tool from taking
over another tool's key. A map is immutable: a rebuild returns a new one, so swapping it in
is atomic.

Spec: docs/spec/addressing.md (Path, The route rule, Building the map, Allow and refusals)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Mapping

from .methods import ALL_METHODS, ToolContract, admissible_methods

logger = logging.getLogger("webspec.routes")

# PA-4's closed table: the first word of a tool's name -> (method, definer on POST).
TABLE: dict[str, tuple[str, str]] = {
    **dict.fromkeys(("get", "read", "list", "search"), ("GET", "")),
    **dict.fromkeys(("set", "replace"), ("PUT", "")),
    **dict.fromkeys(("update", "modify", "patch", "append", "rename"), ("PATCH", "")),
    **dict.fromkeys(("delete", "remove"), ("DELETE", "")),
    **dict.fromkeys(("create", "add"), ("POST", "CREATE")),
    "send": ("POST", "SEND"),
    "upload": ("POST", "UPLOAD"),
    "trigger": ("POST", "TRIGGER"),
}

# An entry's key: its method, its path with the surrounding slashes trimmed, and on POST its definer.
Key = tuple[str, str, str]


@dataclass(frozen=True)
class Entry:
    method: str
    path: str          # "/{rest}" or "/{tool name}", verbatim
    definer: str = ""  # the action verb on POST, "" on every other method
    fallback: bool = False


@dataclass(frozen=True)
class Refusal:
    status: int
    error: str
    detail: str = ""
    allow: str | None = None     # the path's Allow, on a 405
    tools: tuple[str, ...] = ()  # the tools on a closed key; binding_changed names (pinned, now)


@dataclass(frozen=True)
class BindingEvent:
    """A rebuild's finding for the audit log (AU-5)."""

    kind: str          # "binding_changed" | "binding_confirmed"
    binding: str       # the key string, "POST /email CREATE"
    pinned_tool: str
    tool: str
    method: str = ""
    path: str = ""
    definer: str = ""


def _admissible(method: str, contract: ToolContract) -> bool:
    """MB-1 admits it, and it is not PUT or DELETE for a non-idempotent tool (RFC 9110 §9.2.2)."""
    return method in admissible_methods(contract) and (contract.idempotent or method not in ("PUT", "DELETE"))


def _fallback_method(contract: ToolContract) -> str:
    if contract.read_only:
        return "GET"
    if contract.destructive and contract.idempotent:
        return "PUT"
    return "POST"


def route(tool_name: str, contract: ToolContract) -> Entry:
    """PA-4: the entry the route rule gives one tool, from its name and its contract alone."""
    first, _, rest = tool_name.partition("_")
    if rest and first in TABLE:
        (method, definer), path = TABLE[first], f"/{rest}"
    else:
        method, definer, path = "POST", "INVOKE", f"/{tool_name}"
    if _admissible(method, contract):
        return Entry(method, path, definer)
    method = _fallback_method(contract)
    return Entry(method, f"/{tool_name}", "INVOKE" if method == "POST" else "", fallback=True)


def key_of(entry: Entry) -> Key:
    return entry.method, entry.path.strip("/"), entry.definer


def key_string(key: Key) -> str:
    """The form of a config ``bindings`` key: ``METHOD /path``, plus `` DEFINER`` on POST."""
    method, path, definer = key
    return f"{method} /{path}" + (f" {definer}" if method == "POST" else "")


def _reachable(key: Key) -> bool:
    """PA-1/PA-6: a key whose trimmed path is empty or holds a "/" matches no request."""
    return bool(key[1]) and "/" not in key[1]


def _shared(entries: Mapping[str, Entry]) -> dict[Key, list[str]]:
    by_key: dict[Key, list[str]] = defaultdict(list)
    for name in sorted(entries):
        by_key[key_of(entries[name])].append(name)
    return {k: names for k, names in by_key.items() if len(names) > 1}


@dataclass(frozen=True)
class RouteMap:
    destination: str
    contracts: Mapping[str, ToolContract]  # the tools and contracts it was built from
    bindings: Mapping[str, str]            # the destination's config ``bindings`` it read
    entries: Mapping[str, Entry]           # every tool's entry, moved or not
    moved: frozenset[str]                  # tools the first build moved to their full names
    pins: Mapping[Key, str]                # binding pins: key -> the tool it is bound to
    keys: Mapping[Key, str | Refusal]      # every reachable key: its tool, or why it is closed
    events: tuple[BindingEvent, ...] = ()  # what this build found, for the audit log (AU-5)

    @classmethod
    def build(cls, destination: str, tools: Mapping[str, ToolContract]) -> "RouteMap":
        """PA-6: the first build. Colliders move to their full names until no entry can move."""
        entries = {name: route(name, contract) for name, contract in tools.items()}
        moved: set[str] = set()
        while movable := [n for names in _shared(entries).values() for n in names if entries[n].path != f"/{n}"]:
            for name in movable:
                entries[name] = replace(entries[name], path=f"/{name}")
            moved.update(movable)
        if moved:
            logger.info("%s: the first route map moved colliding tools to their full names (PA-6): %s",
                        destination, ", ".join(sorted(moved)))
        return cls._assemble(destination, tools, {}, entries, frozenset(moved), {}, None)

    def rebuild(self, tools: Mapping[str, ToolContract], bindings: Mapping[str, str] | None = None) -> "RouteMap":
        """PA-7/PA-8: every re-list and config reload. Nothing moves; shared keys and changed bindings close."""
        bindings = dict(bindings or {})
        entries = {}
        for name, contract in tools.items():
            entry = route(name, contract)
            # A moved tool stays at its full name unless a bindings entry returns it to its key.
            if name in self.moved and bindings.get(key_string(key_of(entry))) != name:
                entry = replace(entry, path=f"/{name}")
            entries[name] = entry
        return self._assemble(self.destination, tools, bindings, entries, self.moved, self.pins, self)

    @classmethod
    def _assemble(cls, destination, tools, bindings, entries, moved, pins, previous) -> "RouteMap":
        by_key: dict[Key, list[str]] = defaultdict(list)
        unreachable = []
        for name in sorted(entries):
            k = key_of(entries[name])
            (by_key[k] if _reachable(k) else unreachable).append(name)
        if unreachable and (previous is None or set(unreachable) - set(previous.unreachable)):
            logger.warning("%s: no request can reach %s, whose path is empty or has more than one segment (PA-6)",
                           destination, ", ".join(unreachable))
        pins, keys, events = dict(pins), {}, []
        for k, names in by_key.items():
            ks = key_string(k)
            was = previous.keys.get(k) if previous is not None else None
            if len(names) > 1:
                keys[k] = Refusal(503, "route_collision", f"{ks} is shared by {', '.join(names)}; the gateway "
                                                          "picks none of them", tools=tuple(names))
                if was != keys[k]:
                    logger.warning("%s: %s is closed, shared by %s (PA-7)", destination, ks, ", ".join(names))
                continue
            tool, pinned = names[0], pins.get(k)
            if pinned in (None, tool):
                pins[k] = keys[k] = tool
                continue
            event = dict(binding=ks, pinned_tool=pinned, tool=tool, method=k[0], path=f"/{k[1]}", definer=k[2])
            closed = Refusal(503, "binding_changed",
                             f"{ks} was bound to {pinned} and now resolves to {tool}; it stays closed until the "
                             f"destination's bindings confirm it", tools=(pinned, tool))
            if was != closed:
                logger.warning("%s: %s was bound to %s and now resolves to %s (PA-8)", destination, ks, pinned, tool)
                events.append(BindingEvent("binding_changed", **event))
            if bindings.get(ks) == tool:
                pins[k] = keys[k] = tool
                events.append(BindingEvent("binding_confirmed", **event))
            else:
                keys[k] = closed
        return cls(destination, dict(tools), dict(bindings), entries, moved, pins, keys, tuple(events))

    @property
    def unreachable(self) -> list[str]:
        return [name for name, e in self.entries.items() if not _reachable(key_of(e))]

    def table(self, path: str | None = None) -> list[tuple[Key, str | Refusal]]:
        """Every key on ``path`` (every key, without one) with its tool or refusal, in canonical order."""
        trimmed = None if path is None else path.strip("/")
        rows = [(k, v) for k, v in self.keys.items() if trimmed is None or k[1] == trimmed]
        return sorted(rows, key=lambda row: (row[0][1], ALL_METHODS.index(row[0][0]), row[0][2]))

    def allow(self, path: str) -> str | None:
        """PA-9: HEAD, OPTIONS and the methods mapped on the path (closed keys count); None if none is."""
        mapped = {k[0] for k, _ in self.table(path)}
        return ", ".join(m for m in ALL_METHODS if m in mapped | {"HEAD", "OPTIONS"}) if mapped else None

    def resolve(self, method: str, path: str, definer: str = "") -> str | Refusal:
        """PA-10 steps 1 to 4: the tool, or the refusal. ``definer`` is the canonical POST verb."""
        rows = dict(self.table(path))
        if not rows:
            return Refusal(404, "tool_not_found", f"{path} maps no tool")
        if method not in {k[0] for k in rows}:
            allow = self.allow(path)
            return Refusal(405, "method_not_allowed", f"{path} does not map {method}; it allows {allow}", allow)
        target = rows.get((method, path.strip("/"), definer if method == "POST" else ""))
        if target is None:  # only POST, whose definer is part of the key, gets here
            actions = ", ".join(k[2] for k in rows if k[0] == "POST")
            return Refusal(400, "unknown_action", f"{path} maps no POST action {definer or '(none)'}; "
                                                  f"OPTIONS {path} lists the actions it maps: {actions}")
        return target

    def routes(self) -> dict[str, str]:
        """Every open key as its key string, mapped to its tool."""
        return {key_string(k): v for k, v in self.keys.items() if isinstance(v, str)}
