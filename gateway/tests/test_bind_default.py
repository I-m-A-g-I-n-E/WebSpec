import webspec.__main__ as m


def test_default_bind_is_localhost(monkeypatch):
    monkeypatch.delenv("WEBSPEC_HOST", raising=False)
    assert m.resolve_bind_host() == "127.0.0.1"


def test_bind_override(monkeypatch):
    monkeypatch.setenv("WEBSPEC_HOST", "0.0.0.0")
    assert m.resolve_bind_host() == "0.0.0.0"
