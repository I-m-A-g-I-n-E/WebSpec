import time
from webspec.guard import compute_clearance_token, validate_clearance_token

def test_clearance_token_roundtrip(session_key):
    ts = str(int(time.time()))
    token = compute_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, ts)
    assert isinstance(token, str)
    assert len(token) == 8  # 4 bytes hex

    result = validate_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, f"{token}:{ts}")
    assert result is None  # None = success

def test_clearance_token_wrong_tool(session_key):
    ts = str(int(time.time()))
    token = compute_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, ts)
    result = validate_clearance_token(session_key, "list_vaults", {}, f"{token}:{ts}")
    assert result == "clearance_invalid"

def test_clearance_token_wrong_args(session_key):
    ts = str(int(time.time()))
    token = compute_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, ts)
    result = validate_clearance_token(session_key, "read", {"reference": "op://Other/Item/f"}, f"{token}:{ts}")
    assert result == "clearance_invalid"

def test_clearance_token_expired(session_key):
    ts = str(int(time.time()) - 60)  # 60 seconds ago
    token = compute_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, ts)
    result = validate_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, f"{token}:{ts}")
    assert result == "clearance_expired"

def test_clearance_token_malformed(session_key):
    result = validate_clearance_token(session_key, "read", {}, "garbage")
    assert result == "clearance_malformed"

def test_clearance_token_missing(session_key):
    result = validate_clearance_token(session_key, "read", {}, None)
    assert result == "clearance_missing"

def test_clearance_args_canonicalization(session_key):
    """Arg order doesn't matter — keys are sorted."""
    ts = str(int(time.time()))
    token1 = compute_clearance_token(session_key, "get_item", {"vault": "A", "item": "B"}, ts)
    token2 = compute_clearance_token(session_key, "get_item", {"item": "B", "vault": "A"}, ts)
    assert token1 == token2
