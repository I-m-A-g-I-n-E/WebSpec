import os

import pytest


@pytest.fixture
def session_key():
    """Fixed 32-byte session key for deterministic tests."""
    return b"\x01" * 32


@pytest.fixture(autouse=True)
def _isolate_gateway_state(tmp_path, monkeypatch):
    """Keep every test's audit log, idempotency keys, approvals and contract pins private.

    Also points the gateway at a nonexistent config so no test ever reads ~/.claude.json, and
    gives PATH back after each test: webspec-ctl's main() sets it to SAFE_PATH when it runs as
    root (DP-1, DP-4), right for its own process, but tests call it in-process, and a root run
    then left SAFE_PATH to every later test.
    """
    from webspec import approval, guard, handlers, idempotency
    from webspec.methods import ContractPins

    monkeypatch.setenv("PATH", os.environ.get("PATH", os.defpath))
    # Set by the Linux unit; in a test's environment it would stop main() without a key.
    monkeypatch.delenv("WEBSPEC_REQUIRE_GUARD_KEY", raising=False)
    monkeypatch.setenv("WEBSPEC_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(tmp_path / "no-such-claude.json"))
    monkeypatch.setenv("WEBSPEC_ENV_FILE", str(tmp_path / "no-such.env"))
    monkeypatch.setattr(handlers, "contract_pins", ContractPins())
    monkeypatch.setattr(idempotency, "store", idempotency.IdempotencyStore())
    monkeypatch.setattr(approval, "store", approval.ApprovalStore())
    monkeypatch.setattr(guard, "spent_clearances", guard._SpentClearances())
