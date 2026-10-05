import json
import tempfile
from pathlib import Path

# We need to test ufo.py which lives in services/op-auth, so add to path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "op-auth"))

from ufo import get_tier, is_run_allowed, write_audit_entry, TIERS

def test_tier_classification():
    assert get_tier("list_vaults") == "open"
    assert get_tier("read") == "sensitive"
    assert get_tier("list_items") == "sensitive"
    assert get_tier("get_item") == "sensitive"
    assert get_tier("run") == "dangerous"

def test_unknown_tool_is_dangerous():
    assert get_tier("unknown_tool") == "dangerous"

def test_run_allowlist():
    assert is_run_allowed("vault", ["list"]) is True
    assert is_run_allowed("item", ["list", "--vault", "Personal"]) is True
    assert is_run_allowed("item", ["get", "MyItem", "--vault", "Personal"]) is True
    assert is_run_allowed("document", ["get", "MyDoc"]) is True

def test_run_blocklist():
    assert is_run_allowed("account", ["list"]) is False
    assert is_run_allowed("item", ["delete", "MyItem"]) is False
    assert is_run_allowed("item", ["edit", "MyItem"]) is False
    assert is_run_allowed("user", ["list"]) is False
    assert is_run_allowed("events-api", ["create"]) is False
    assert is_run_allowed("inject", ["--template"]) is False

def test_audit_log(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    write_audit_entry(
        log_path=log_path,
        tool="read",
        args={"reference": "op://V/I/f"},
        tier="sensitive",
        provenance="human:abcd->gateway",
        outcome="allowed",
        clearance_valid=True,
    )
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "read"
    assert entry["outcome"] == "allowed"
    assert "timestamp" in entry

def test_audit_log_append(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    for i in range(3):
        write_audit_entry(log_path=log_path, tool=f"tool_{i}", args={},
                          tier="open", provenance="", outcome="allowed", clearance_valid=True)
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 3
