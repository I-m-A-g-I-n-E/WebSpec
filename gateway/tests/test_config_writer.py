"""Tests for config_writer: safe read-modify-write of ~/.claude.json and ~/.env."""

import json
import pytest
from pathlib import Path

from webspec.config_writer import (
    add_service,
    remove_service,
    list_services,
    add_env_var,
    remove_env_var,
)


@pytest.fixture
def config_file(tmp_path):
    """Create a temp ~/.claude.json with one existing service."""
    p = tmp_path / "claude.json"
    data = {
        "mcpServers": {
            "existing-svc": {
                "type": "http",
                "url": "http://localhost:9000/mcp",
                "guard": True,
            }
        }
    }
    p.write_text(json.dumps(data, indent=2))
    return p


@pytest.fixture
def env_file(tmp_path):
    """Create a temp .env file."""
    p = tmp_path / ".env"
    p.write_text("EXISTING_KEY=existing_value\n")
    return p


# ── add_service ──


def test_add_service_new(config_file):
    add_service("new-svc", {"type": "http", "url": "http://localhost:8080"}, path=config_file)
    services = list_services(config_file)
    assert "new-svc" in services
    assert services["new-svc"]["url"] == "http://localhost:8080"
    # Existing service still there
    assert "existing-svc" in services


def test_add_service_idempotent_update(config_file):
    add_service("existing-svc", {"type": "http", "url": "http://new-url:9999"}, path=config_file)
    services = list_services(config_file)
    assert services["existing-svc"]["url"] == "http://new-url:9999"


def test_add_service_creates_backup(config_file):
    add_service("another", {"type": "stdio", "command": "echo"}, path=config_file)
    bak = config_file.with_suffix(".json.bak")
    assert bak.exists()
    # Backup should have old content (only existing-svc)
    old_data = json.loads(bak.read_text())
    assert "another" not in old_data.get("mcpServers", {})


def test_add_service_atomic_write(config_file):
    """After write, no .tmp file should remain."""
    add_service("test", {"type": "http", "url": "http://x"}, path=config_file)
    tmp = config_file.with_suffix(".json.tmp")
    assert not tmp.exists()


def test_add_service_preserves_other_keys(tmp_path):
    """Non-mcpServers keys in ~/.claude.json are preserved."""
    p = tmp_path / "claude.json"
    data = {"mcpServers": {}, "otherKey": "preserved"}
    p.write_text(json.dumps(data))
    add_service("svc", {"type": "http", "url": "http://x"}, path=p)
    result = json.loads(p.read_text())
    assert result["otherKey"] == "preserved"


# ── remove_service ──


def test_remove_service_existing(config_file):
    remove_service("existing-svc", path=config_file)
    services = list_services(config_file)
    assert "existing-svc" not in services


def test_remove_service_missing_noop(config_file):
    """Removing a non-existent service is a no-op."""
    remove_service("nonexistent", path=config_file)
    services = list_services(config_file)
    assert "existing-svc" in services


def test_remove_service_creates_backup(config_file):
    remove_service("existing-svc", path=config_file)
    bak = config_file.with_suffix(".json.bak")
    assert bak.exists()


# ── list_services ──


def test_list_services(config_file):
    services = list_services(config_file)
    assert "existing-svc" in services
    assert services["existing-svc"]["type"] == "http"


def test_list_services_empty(tmp_path):
    p = tmp_path / "claude.json"
    p.write_text(json.dumps({"mcpServers": {}}))
    services = list_services(p)
    assert services == {}


# ── add_env_var ──


def test_add_env_var_new(env_file):
    add_env_var("NEW_KEY", "new_value", path=env_file)
    content = env_file.read_text()
    assert "NEW_KEY=new_value" in content
    assert "EXISTING_KEY=existing_value" in content


def test_add_env_var_idempotent(env_file):
    add_env_var("EXISTING_KEY", "should_not_overwrite", path=env_file)
    content = env_file.read_text()
    assert "EXISTING_KEY=existing_value" in content
    assert content.count("EXISTING_KEY=") == 1


def test_add_env_var_empty_value(env_file):
    add_env_var("PLACEHOLDER", path=env_file)
    content = env_file.read_text()
    assert "PLACEHOLDER=" in content


def test_add_env_var_creates_file(tmp_path):
    p = tmp_path / ".env"
    assert not p.exists()
    add_env_var("KEY", "val", path=p)
    assert p.exists()
    assert "KEY=val" in p.read_text()


# ── remove_env_var ──


def test_remove_env_var_existing(env_file):
    remove_env_var("EXISTING_KEY", path=env_file)
    content = env_file.read_text()
    assert "EXISTING_KEY" not in content


def test_remove_env_var_missing_noop(env_file):
    remove_env_var("NONEXISTENT", path=env_file)
    content = env_file.read_text()
    assert "EXISTING_KEY=existing_value" in content


def test_remove_env_var_no_file(tmp_path):
    """Removing from non-existent .env is a no-op."""
    p = tmp_path / ".env"
    remove_env_var("KEY", path=p)  # Should not raise
    assert not p.exists()
