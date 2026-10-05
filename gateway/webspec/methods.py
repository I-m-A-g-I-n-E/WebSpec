"""Per-method profiles: tool contracts, method binding, and level-gated request rules.

Normative spec: docs/http-methods/method-profiles.md

Three ideas live here:

1. **Tool contract.** Every tool has a contract (read-only? destructive? idempotent?
   open-world? tier?). It comes from the operator's per-tool override if present,
   else from the server's MCP ToolAnnotations, else from the MCP defaults — which
   describe the strictest possible tool (not read-only, destructive, non-idempotent,
   open-world). An unannotated tool is therefore treated as dangerous-to-call.

2. **Method binding.** The *gateway* — not the caller — decides which HTTP methods
   may invoke a tool, as a function of its contract. A caller can never reach a
   destructive tool through GET (whose rules are weakest). Mismatch → 405 + Allow.

3. **Requirements are a union.** What a request must carry is
   ``method rules ∪ contract rules ∪ tier rules``, each gated by the service's
   security level (0–4). Raising the level only ever adds requirements.

Contracts are monotone over time: once the gateway has seen a tool, a later
``tools/list`` can tighten its contract but never loosen it (``ToolContract.join``).
Only an operator override can loosen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

logger = logging.getLogger("webspec.methods")

# ── Levels (the security dial) ──

L0_LOCAL = 0       # loopback only; structural grammar rules only
L1_SIGNED = 1      # + guard HMAC + single-use audience-bound nonce (enforced in app.py)
L2_BOUND = 2       # + Tier-2 bookend on unsafe methods; Idempotency-Key on POST/PATCH
L3_CLEARED = 3     # + UFO clearance token for sensitive/dangerous tiers and non-read-only tools
L4_WITNESSED = 4   # + human approval (Ed25519 signature) for destructive tools / dangerous tier
MIN_LEVEL, MAX_LEVEL = L0_LOCAL, L4_WITNESSED
LEVEL_NAMES = {0: "local", 1: "signed", 2: "bound", 3: "cleared", 4: "witnessed"}

# ── Methods ──

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DISCOVERY_METHODS = frozenset({"HEAD", "OPTIONS"})  # never invoke a tool
INVOKING_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
ALL_METHODS = ("HEAD", "OPTIONS") + INVOKING_METHODS  # canonical order (routes, CORS, Allow)

# ── Tiers ──

TIERS = ("open", "sensitive", "dangerous")
_TIER_RANK = {t: i for i, t in enumerate(TIERS)}
TIER_META_KEY = "webspec/tier"  # optional Tool._meta key a server may use to *raise* a tier

OVERRIDE_FIELDS = frozenset({"read_only", "destructive", "idempotent", "open_world", "tier"})


@dataclass(frozen=True)
class ToolContract:
    """What a tool is allowed to be called as. Defaults = MCP defaults = strictest."""

    read_only: bool = False
    destructive: bool = True
    idempotent: bool = False
    open_world: bool = True
    tier: str = "sensitive"
    source: str = field(default="default", compare=False)

    @property
    def risk(self) -> int:
        """0 = read-only, 1 = additive mutation, 2 = destructive mutation."""
        if self.read_only:
            return 0
        return 2 if self.destructive else 1

    @property
    def tier_rank(self) -> int:
        return _TIER_RANK[self.tier]

    def join(self, other: "ToolContract") -> "ToolContract":
        """Least upper bound in the strictness lattice: at least as strict as both."""
        read_only = self.read_only and other.read_only
        return ToolContract(
            read_only=read_only,
            destructive=(not read_only) and (self.destructive or other.destructive),
            idempotent=self.idempotent and other.idempotent,
            open_world=self.open_world or other.open_world,
            tier=TIERS[max(self.tier_rank, other.tier_rank)],
            source=self.source if self.source == other.source else "pinned",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "destructive": self.destructive,
            "idempotent": self.idempotent,
            "open_world": self.open_world,
            "tier": self.tier,
            "source": self.source,
        }


STRICTEST = ToolContract(tier="dangerous", source="strictest")


def admissible_methods(contract: ToolContract) -> frozenset[str]:
    """Which invoking methods may reach a tool with this contract.

    read-only                 → GET
    additive (non-destructive)→ POST, PATCH (+ PUT if idempotent)
    destructive               → DELETE  (+ PUT if idempotent, else POST)

    HEAD and OPTIONS are always available on a tool path but never invoke it.
    """
    if contract.read_only:
        return frozenset({"GET"})
    if not contract.destructive:
        methods = {"POST", "PATCH"}
        if contract.idempotent:
            methods.add("PUT")
        return frozenset(methods)
    return frozenset({"DELETE", "PUT" if contract.idempotent else "POST"})


def allow_header(contract: ToolContract) -> str:
    """RFC 9110 Allow header value for a tool path."""
    allowed = admissible_methods(contract) | DISCOVERY_METHODS
    return ", ".join(m for m in ALL_METHODS if m in allowed)


def contract_from_annotations(annotations: Any, meta: Mapping[str, Any] | None = None) -> ToolContract:
    """Derive a contract from MCP ToolAnnotations (+ optional ``_meta`` tier).

    Absent annotations ⇒ MCP defaults (strict). Absent individual hints take the MCP
    default for that hint. A server-declared tier may only *raise* the derived tier.
    """
    if annotations is None:
        contract = ToolContract(source="default")
    else:
        read_only = getattr(annotations, "readOnlyHint", None) is True
        if read_only:
            contract = ToolContract(
                read_only=True,
                destructive=False,
                idempotent=True,
                open_world=getattr(annotations, "openWorldHint", None) is not False,
                tier="open",
                source="annotations",
            )
        else:
            contract = ToolContract(
                read_only=False,
                destructive=getattr(annotations, "destructiveHint", None) is not False,
                idempotent=getattr(annotations, "idempotentHint", None) is True,
                open_world=getattr(annotations, "openWorldHint", None) is not False,
                tier="sensitive",
                source="annotations",
            )

    declared = (meta or {}).get(TIER_META_KEY) if isinstance(meta, Mapping) else None
    if isinstance(declared, str) and declared in _TIER_RANK and _TIER_RANK[declared] > contract.tier_rank:
        contract = replace(contract, tier=declared)
    return contract


def contract_from_tool(tool: Any) -> ToolContract:
    """Contract for an ``mcp.types.Tool`` (or anything with .annotations / .meta)."""
    return contract_from_annotations(getattr(tool, "annotations", None), getattr(tool, "meta", None))


def apply_override(contract: ToolContract, override: Mapping[str, Any] | None) -> ToolContract:
    """Apply an operator override (authoritative; may loosen).

    Unknown keys or wrongly-typed values make the whole override invalid, and an
    invalid override fails closed to the strictest contract.
    """
    if not override:
        return contract
    if not isinstance(override, Mapping) or not set(override) <= OVERRIDE_FIELDS:
        logger.error("Invalid tool override %r — failing closed to strictest contract", override)
        return STRICTEST
    for key in ("read_only", "destructive", "idempotent", "open_world"):
        if key in override and not isinstance(override[key], bool):
            logger.error("Override field %s must be bool, got %r — failing closed", key, override[key])
            return STRICTEST
    if "tier" in override and (not isinstance(override["tier"], str) or override["tier"] not in _TIER_RANK):
        logger.error("Override tier must be one of %s, got %r — failing closed", TIERS, override["tier"])
        return STRICTEST

    base = contract
    if override.get("read_only") is False and contract.read_only:
        # Un-marking a tool read-only must land on the strict MCP defaults, not on the
        # read-only normalization (non-destructive, idempotent, open) — that would be
        # *weaker* than an unannotated tool and drop the level-4 gate.
        base = ToolContract()
    merged = replace(base, source="override", **{k: override[k] for k in override})
    if merged.read_only:
        # A read-only tool cannot be destructive; keep the struct internally consistent.
        merged = replace(merged, destructive=False, idempotent=True)
    return merged


class ContractPins:
    """Remembers the strictest contract seen per (service, tool) since startup.

    Defends against a server "rug pull": re-listing a tool with looser annotations
    (e.g. flipping ``readOnlyHint`` to true to unlock GET). The effective observed
    contract is ``pinned ⊔ observed``. Operator overrides are applied *after* pinning.
    """

    def __init__(self) -> None:
        self._pins: dict[tuple[str, str], ToolContract] = {}
        self._warned: set[tuple[str, str, ToolContract]] = set()

    def observe(self, service: str, tool_name: str, observed: ToolContract) -> ToolContract:
        key = (service, tool_name)
        pinned = self._pins.get(key)
        if pinned is None:
            effective = observed
        else:
            effective = pinned.join(observed)
            if effective != observed and (service, tool_name, observed) not in self._warned:
                self._warned.add((service, tool_name, observed))
                logger.warning(
                    "Contract loosening blocked for %s/%s: observed %s, keeping %s",
                    service, tool_name, observed.as_dict(), effective.as_dict(),
                )
        self._pins[key] = effective
        return effective

    # Pins live for the process lifetime and deliberately survive a service being
    # removed from config and re-added (otherwise editing the config would be a way to
    # reset them). TODO(C): persist pins across restarts (operator-approved contract
    # snapshot file); see docs/ROADMAP-C.md. Until then a restart re-trusts the first
    # tools/list.


def effective_contract(
    pins: ContractPins,
    service: str,
    tool: Any,
    overrides: Mapping[str, Any] | None = None,
) -> ToolContract:
    """observed (annotations) → pinned join → operator override."""
    observed = contract_from_tool(tool)
    pinned = pins.observe(service, tool.name, observed)
    if overrides and not isinstance(overrides, Mapping):
        logger.error("Service %s: 'tools' overrides must be an object — failing closed", service)
        return STRICTEST
    override = (overrides or {}).get(tool.name)
    return apply_override(pinned, override)


# ── Requirements ──


@dataclass(frozen=True)
class Requirements:
    """What a request must carry. ``guard`` is enforced in app.py, before the handler."""

    guard: bool = False            # X-WebSpec-Guard HMAC + single-use nonce
    definer: bool = False          # X-Gimme-Definer from this method's family
    bookend: bool = False          # Tier-2 bookend HMAC in the definer header
    idempotency_key: bool = False  # Idempotency-Key header (POST/PATCH)
    empty_body: bool = False       # request body MUST be empty (GET)
    clearance: bool = False        # X-UFO-Clearance token bound to (tool, args)
    approval: bool = False         # X-WebSpec-Approval: human Ed25519 signature

    def as_dict(self) -> dict[str, bool]:
        return {
            "guard": self.guard,
            "definer": self.definer,
            "bookend": self.bookend,
            "idempotency_key": self.idempotency_key,
            "empty_body": self.empty_body,
            "clearance": self.clearance,
            "approval": self.approval,
        }


def requirements(method: str, contract: ToolContract, level: int) -> Requirements:
    """Effective requirements = method rules ∪ contract rules ∪ tier rules, gated by level.

    Precondition: ``method`` is admissible for ``contract`` (checked by the caller).
    """
    method = method.upper()
    unsafe = method not in SAFE_METHODS
    return Requirements(
        guard=level >= L1_SIGNED,
        # Method rules
        definer=unsafe,
        bookend=unsafe and level >= L2_BOUND,
        empty_body=method == "GET",
        # Contract rules. The key follows the *tool*, not the method: a non-idempotent
        # tool needs one whichever unsafe method reaches it (DELETE included).
        idempotency_key=unsafe and not contract.idempotent and level >= L2_BOUND,
        # Tier rules
        clearance=level >= L3_CLEARED and (contract.risk >= 1 or contract.tier_rank >= 1),
        # A human witnesses anything destructive, anything dangerous, and any mutation
        # that reaches the open world (the exfiltration step: read a secret, then send it).
        approval=level >= L4_WITNESSED and (
            contract.risk >= 2 or contract.tier == "dangerous" or (contract.risk >= 1 and contract.open_world)
        ),
    )


def describe(contract: ToolContract, level: int) -> dict[str, Any]:
    """Machine-readable contract + per-method requirements, for OPTIONS discovery."""
    methods = sorted(admissible_methods(contract))
    return {
        "methods": methods,
        "contract": contract.as_dict(),
        "level": level,
        "level_name": LEVEL_NAMES[level],
        "requirements": {m: requirements(m, contract, level).as_dict() for m in methods},
    }


def parse_level(raw: Any, *, guard: bool, service: str = "?") -> int:
    """Resolve a service's level from config.

    Absent → 1 if guarded else 0 (backward compatible with ``guard``). Invalid →
    fail closed to the maximum level. ``guard: true`` forces level ≥ 1.
    """
    if raw is None:
        level = L1_SIGNED if guard else L0_LOCAL
    elif isinstance(raw, bool) or not isinstance(raw, int) or not MIN_LEVEL <= raw <= MAX_LEVEL:
        logger.error("Service %s: invalid level %r — failing closed to level %d", service, raw, MAX_LEVEL)
        level = MAX_LEVEL
    else:
        level = raw
    if guard:
        level = max(level, L1_SIGNED)
    return level
