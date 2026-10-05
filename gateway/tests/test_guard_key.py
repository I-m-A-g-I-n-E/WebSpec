"""Guard key is sourced from the environment (populated from a password manager), never a file."""
import hashlib
import pytest
import webspec.config as config
from webspec.config import GuardKeyError, get_session_key


@pytest.fixture(autouse=True)
def _reset_ephemeral(monkeypatch):
    # Ensure a clean env + reset the cached dev key between tests.
    monkeypatch.delenv("WEBSPEC_GUARD_KEY", raising=False)
    monkeypatch.delenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", raising=False)
    config._dev_ephemeral_key = None
    yield
    config._dev_ephemeral_key = None


def test_hex_key_decoded(monkeypatch):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "01" * 32)  # 64 hex chars
    assert get_session_key() == b"\x01" * 32


def test_passphrase_key_derived(monkeypatch):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "hunter2")
    assert get_session_key() == hashlib.sha256(b"hunter2").digest()
    assert len(get_session_key()) == 32


def test_missing_key_fails_closed():
    with pytest.raises(GuardKeyError):
        get_session_key()


def test_dev_ephemeral_is_stable(monkeypatch):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    k1 = get_session_key()
    k2 = get_session_key()
    assert k1 == k2 and len(k1) == 32


def test_no_session_key_file_created(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "02" * 32)
    get_session_key()
    assert not (tmp_path / ".webspec" / "session.key").exists()


def test_whitespace_only_key_fails_closed(monkeypatch):
    # A blank/whitespace-only key must not silently derive to sha256(b"") — a
    # publicly-known key. It should be treated the same as missing.
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "   ")
    with pytest.raises(GuardKeyError):
        get_session_key()
