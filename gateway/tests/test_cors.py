import webspec.app as appmod


def test_cors_empty_by_default(monkeypatch):
    monkeypatch.delenv("WEBSPEC_CORS_ORIGINS", raising=False)
    assert appmod.cors_origins() == []


def test_cors_parses_list(monkeypatch):
    monkeypatch.setenv("WEBSPEC_CORS_ORIGINS", "https://a.example, https://b.example")
    assert appmod.cors_origins() == ["https://a.example", "https://b.example"]
