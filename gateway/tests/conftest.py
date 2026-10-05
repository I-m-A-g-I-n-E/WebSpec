import pytest


@pytest.fixture
def session_key():
    """Fixed 32-byte session key for deterministic tests."""
    return b"\x01" * 32


@pytest.fixture(autouse=True)
def _isolate_gateway_state(tmp_path, monkeypatch):
    """Keep every test's audit log, idempotency keys, approvals and contract pins private.

    Also points the gateway at a nonexistent config so no test ever reads ~/.claude.json.
    """
    from webspec import approval, handlers, idempotency
    from webspec.methods import ContractPins

    monkeypatch.setenv("WEBSPEC_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(tmp_path / "no-such-claude.json"))
    monkeypatch.setattr(handlers, "contract_pins", ContractPins())
    monkeypatch.setattr(idempotency, "store", idempotency.IdempotencyStore())
    monkeypatch.setattr(approval, "store", approval.ApprovalStore())
