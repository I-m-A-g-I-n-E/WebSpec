"""ServiceRegistry must degrade to an empty registry, not crash, on an unreadable
or malformed config path (e.g. a directory bind-mount, or invalid JSON)."""
from pathlib import Path

from webspec.config import ServiceRegistry


def test_registry_survives_directory_config_path(tmp_path):
    # Simulates a Docker bind-mount that created an empty directory instead of a file.
    config_dir = tmp_path / "claude.json"
    config_dir.mkdir()
    reg = ServiceRegistry(config_path=config_dir)
    assert reg.names() == []


def test_registry_survives_malformed_json(tmp_path):
    config_file = tmp_path / "claude.json"
    config_file.write_text("{not valid json")
    reg = ServiceRegistry(config_path=config_file)
    assert reg.names() == []


def test_registry_still_loads_valid_config(tmp_path):
    import json

    config_file = tmp_path / "claude.json"
    config_file.write_text(json.dumps({"mcpServers": {"svc-x": {"type": "http", "url": "http://x"}}}))
    reg = ServiceRegistry(config_path=config_file)
    assert "svc-x" in reg.names()
