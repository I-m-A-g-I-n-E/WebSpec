# gateway/tests/test_opauth_ufo_integration.py
"""Test that op-auth server.py enforces UFO tiers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "op-auth"))

from ufo import get_tier, is_run_allowed

def test_run_rejects_delete():
    """run() with item delete must be denied by allowlist."""
    assert is_run_allowed("item", ["delete", "MyItem"]) is False

def test_run_rejects_edit():
    """run() with item edit must be denied by allowlist."""
    assert is_run_allowed("item", ["edit", "MyItem"]) is False

def test_run_allows_item_list():
    assert is_run_allowed("item", ["list", "--vault", "Personal"]) is True

def test_all_tools_have_tiers():
    """Every tool declared in server.py has a tier in ufo.py."""
    expected_tools = {"read", "list_vaults", "list_items", "get_item", "run"}
    for tool in expected_tools:
        tier = get_tier(tool)
        assert tier in ("open", "sensitive", "dangerous"), f"{tool} has invalid tier: {tier}"
