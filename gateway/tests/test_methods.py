"""Unit tests: tool contracts, the strictness lattice, method binding, requirements, levels."""

import itertools

import pytest
from mcp.types import ToolAnnotations

from webspec.config import ServiceEntry
from webspec.methods import (
    STRICTEST,
    TIERS,
    ContractPins,
    ToolContract,
    admissible_methods,
    allow_header,
    apply_override,
    contract_from_annotations,
    describe,
    parse_level,
    requirements,
)


# ── contract derivation ──

def test_unannotated_tool_gets_strict_mcp_defaults():
    c = contract_from_annotations(None)
    assert (c.read_only, c.destructive, c.idempotent, c.open_world, c.tier) == (False, True, False, True, "sensitive")
    assert admissible_methods(c) == {"POST", "DELETE"}


def test_read_only_annotation():
    c = contract_from_annotations(ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    assert c.read_only and not c.destructive and c.tier == "open" and not c.open_world
    assert admissible_methods(c) == {"GET"}


def test_additive_and_idempotent():
    c = contract_from_annotations(ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    assert admissible_methods(c) == {"POST", "PATCH"}
    c = contract_from_annotations(ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    assert admissible_methods(c) == {"POST", "PATCH", "PUT"}


def test_destructive_idempotent():
    c = contract_from_annotations(ToolAnnotations(destructiveHint=True, idempotentHint=True))
    assert admissible_methods(c) == {"PUT", "DELETE"}


def test_destructive_hint_ignored_when_read_only():
    c = contract_from_annotations(ToolAnnotations(readOnlyHint=True, destructiveHint=True))
    assert c.read_only and not c.destructive


def test_meta_tier_can_only_raise():
    up = contract_from_annotations(ToolAnnotations(readOnlyHint=True), {"webspec/tier": "dangerous"})
    assert up.tier == "dangerous"
    down = contract_from_annotations(ToolAnnotations(readOnlyHint=False), {"webspec/tier": "open"})
    assert down.tier == "sensitive"  # a server can't lower its own tier
    junk = contract_from_annotations(None, {"webspec/tier": "whatever"})
    assert junk.tier == "sensitive"


def test_allow_header_lists_discovery_plus_admissible():
    assert allow_header(contract_from_annotations(ToolAnnotations(readOnlyHint=True))) == "HEAD, OPTIONS, GET"
    assert allow_header(ToolContract()) == "HEAD, OPTIONS, POST, DELETE"


# ── lattice ──

ALL_CONTRACTS = [
    ToolContract(read_only=r, destructive=(not r) and d, idempotent=i or r, open_world=o, tier=t)
    for r, d, i, o, t in itertools.product([True, False], [True, False], [True, False], [True, False], TIERS)
]


def _at_least_as_strict(a: ToolContract, b: ToolContract) -> bool:
    return (a.risk >= b.risk and a.tier_rank >= b.tier_rank
            and (not a.idempotent or b.idempotent) and (a.open_world or not b.open_world))


def test_join_is_an_upper_bound_and_commutative():
    for a, b in itertools.product(ALL_CONTRACTS, repeat=2):
        j = a.join(b)
        assert j == b.join(a)
        assert _at_least_as_strict(j, a) and _at_least_as_strict(j, b)
        assert a.join(a) == a


def test_join_never_unlocks_a_weaker_method_class():
    # Whatever the join, it never admits GET unless both sides were read-only.
    for a, b in itertools.product(ALL_CONTRACTS, repeat=2):
        if "GET" in admissible_methods(a.join(b)):
            assert a.read_only and b.read_only


def test_pins_block_loosening_but_accept_tightening():
    pins = ContractPins()
    ro = ToolContract(read_only=True, destructive=False, idempotent=True, tier="open")
    destructive = ToolContract()
    assert pins.observe("s", "t", ro) == ro
    assert pins.observe("s", "t", destructive) == destructive   # tighten: accepted
    assert pins.observe("s", "t", ro) == destructive            # loosen: blocked
    assert pins.observe("other", "t", ro) == ro                 # pins are per service


# ── overrides ──

def test_override_is_authoritative_and_can_loosen():
    c = apply_override(ToolContract(), {"read_only": True, "tier": "open"})
    assert c.read_only and not c.destructive and c.idempotent and c.source == "override"
    assert admissible_methods(c) == {"GET"}


@pytest.mark.parametrize("bad", [
    {"read_only": "yes"},
    {"tier": "medium"},
    {"methods": ["GET"]},
    ["read_only"],
])
def test_invalid_override_fails_closed(bad):
    assert apply_override(ToolContract(read_only=True, destructive=False), bad) == STRICTEST


# ── requirements ──

def test_requirements_are_monotone_in_level():
    for c in ALL_CONTRACTS:
        for m in admissible_methods(c):
            prev = None
            for level in range(5):
                r = requirements(m, c, level).as_dict()
                if prev is not None:
                    assert all(r[k] >= prev[k] for k in r), (m, c, level)
                prev = r


def test_method_rules():
    ro = ToolContract(read_only=True, destructive=False, idempotent=True, tier="open")
    assert requirements("GET", ro, 4).as_dict() == {
        "guard": True, "definer": False, "bookend": False, "idempotency_key": False,
        "empty_body": True, "clearance": False, "approval": False,
    }
    add = ToolContract(destructive=False)
    r = requirements("POST", add, 2)
    assert r.definer and r.bookend and r.idempotency_key and not r.clearance
    assert not requirements("PUT", ToolContract(destructive=False, idempotent=True), 2).idempotency_key
    assert requirements("PATCH", add, 2).idempotency_key
    # The key follows the tool, not the method: DELETE can't dodge it (review finding).
    assert requirements("DELETE", ToolContract(), 2).idempotency_key
    assert not requirements("DELETE", ToolContract(idempotent=True), 2).idempotency_key
    assert requirements("GET", ro, 1).guard and not requirements("GET", ro, 0).guard


def test_contract_and_tier_rules():
    add = ToolContract(destructive=False, open_world=False)
    assert requirements("POST", add, 3).clearance and not requirements("POST", add, 3).approval
    assert not requirements("POST", add, 4).approval          # closed-world additive: no human needed
    # An open-world mutation is the exfiltration step (read a secret, then send it): witnessed.
    assert requirements("POST", ToolContract(destructive=False, open_world=True), 4).approval
    assert requirements("DELETE", ToolContract(), 4).approval  # destructive: human needed
    sensitive_read = ToolContract(read_only=True, destructive=False, idempotent=True, tier="sensitive")
    assert requirements("GET", sensitive_read, 3).clearance
    dangerous_read = ToolContract(read_only=True, destructive=False, idempotent=True, tier="dangerous")
    assert requirements("GET", dangerous_read, 4).approval


def test_describe_lists_requirements_per_admissible_method():
    d = describe(ToolContract(), 4)
    assert d["methods"] == ["DELETE", "POST"] and set(d["requirements"]) == {"DELETE", "POST"}
    assert d["level_name"] == "witnessed"


# ── levels ──

@pytest.mark.parametrize("raw,guard,expected", [
    (None, False, 0), (None, True, 1), (0, True, 1), (3, False, 3), (4, True, 4),
    (7, False, 4), (-1, False, 4), ("2", False, 4), (True, False, 4),
])
def test_parse_level(raw, guard, expected):
    assert parse_level(raw, guard=guard) == expected


def test_service_entry_keeps_guard_and_level_consistent():
    assert ServiceEntry(name="a", original_name="a", transport_type="stdio", guard=True).level == 1
    e = ServiceEntry(name="a", original_name="a", transport_type="stdio", level=3)
    assert e.guard is True and e.level == 3
    assert ServiceEntry(name="a", original_name="a", transport_type="stdio").guard is False


def test_unmarking_read_only_lands_on_strict_defaults():
    """Review finding: {"read_only": false} used to produce a contract weaker than an unannotated tool."""
    ro = ToolContract(read_only=True, destructive=False, idempotent=True, tier="open", open_world=False)
    c = apply_override(ro, {"read_only": False})
    assert (c.destructive, c.idempotent, c.tier) == (True, False, "sensitive")
    assert admissible_methods(c) == {"POST", "DELETE"} and requirements("DELETE", c, 4).approval


@pytest.mark.parametrize("bad_tier", [["open"], {"t": 1}, 3, None])
def test_non_string_tier_override_fails_closed(bad_tier):
    """Review finding: an unhashable tier crashed discovery for the whole service."""
    assert apply_override(ToolContract(), {"tier": bad_tier}) == STRICTEST
