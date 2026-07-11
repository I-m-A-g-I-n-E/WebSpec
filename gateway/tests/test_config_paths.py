import json
import webspec.config_writer as cw
from webspec.config import ServiceRegistry


def test_default_config_path_env(monkeypatch, tmp_path):
    p = tmp_path / "alt.json"
    monkeypatch.setenv("WEBSPEC_CONFIG", str(p))
    assert cw.default_config_path() == p


def test_registry_reads_env_config(monkeypatch, tmp_path):
    p = tmp_path / "alt.json"
    p.write_text(json.dumps({"mcpServers": {"svc-x": {"type": "http", "url": "http://x"}}}))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(p))
    reg = ServiceRegistry()
    assert "svc-x" in reg.names()
