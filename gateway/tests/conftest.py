import pytest

@pytest.fixture
def session_key():
    """Fixed 32-byte session key for deterministic tests."""
    return b"\x01" * 32
