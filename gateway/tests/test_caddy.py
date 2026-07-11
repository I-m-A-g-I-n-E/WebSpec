"""Tests for caddy: site block generation and sync."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from webspec.caddy import (
    generate_direct_site_block,
    generate_site_block,
    write_site_block,
    remove_site_block,
    sync_caddy_config,
)


# ── generate_site_block ──


def test_generate_site_block_basic():
    block = generate_site_block("supabase", "i-a-m.live")
    assert "http://supabase.localhost:7001" in block
    assert "http://supabase.i-a-m.live:7001" in block
    assert "reverse_proxy localhost:7002" in block
    assert "/var/log/caddy/supabase.log" in block
    assert "format json" in block


def test_generate_site_block_custom_ports():
    block = generate_site_block("svc", "example.com", gateway_port=9000, caddy_port=8080)
    assert "http://svc.localhost:8080" in block
    assert "http://svc.example.com:8080" in block
    assert "reverse_proxy localhost:9000" in block


def test_generate_site_block_has_rate_limit():
    block = generate_site_block("svc", "i-a-m.live", guard=True)
    assert "rate_limit" in block
    assert "zone svc" in block
    assert "events 60" in block
    assert "window 1m" in block


def test_generate_site_block_custom_rate_limit():
    block = generate_site_block("svc", "i-a-m.live", rate_limit=120, rate_window="2m")
    assert "events 120" in block
    assert "window 2m" in block


def test_generate_site_block_non_guard_has_logging():
    block = generate_site_block("open-svc", "i-a-m.live", guard=False)
    assert "log {" in block
    assert "format json" in block


def test_generate_site_block_excludes_health_from_ratelimit():
    block = generate_site_block("svc", "i-a-m.live")
    assert "@notHealth not path /__nonce /__challenge" in block
    assert "rate_limit @notHealth" in block


# ── generate_direct_site_block ──


def test_generate_direct_site_block_basic():
    block = generate_direct_site_block("llms", "i-a-m.live", target_port=20128)
    assert "http://llms.localhost:7001" in block
    assert "http://llms.i-a-m.live:7001" in block
    assert "reverse_proxy localhost:20128" in block
    assert "/var/log/caddy/llms.log" in block
    assert "rate_limit" not in block
    assert "__nonce" not in block


def test_generate_direct_site_block_custom_caddy_port():
    block = generate_direct_site_block("app", "example.com", target_port=3000, caddy_port=8080)
    assert "http://app.localhost:8080" in block
    assert "http://app.example.com:8080" in block
    assert "reverse_proxy localhost:3000" in block


# ── write_site_block / remove_site_block ──


def test_write_site_block(tmp_path):
    content = "test block content"
    p = write_site_block("my-svc", content, conf_dir=tmp_path)
    assert p == tmp_path / "my-svc.caddy"
    assert p.read_text() == content


def test_remove_site_block(tmp_path):
    (tmp_path / "my-svc.caddy").write_text("content")
    remove_site_block("my-svc", conf_dir=tmp_path)
    assert not (tmp_path / "my-svc.caddy").exists()


def test_remove_site_block_missing_noop(tmp_path):
    remove_site_block("nonexistent", conf_dir=tmp_path)  # no error


# ── sync_caddy_config ──


def _mock_registry(services: dict):
    """Create a mock ServiceRegistry with given service names and guard flags."""
    registry = MagicMock()
    registry.names.return_value = list(services.keys())

    def mock_get(name):
        if name not in services:
            return None
        entry = MagicMock()
        entry.guard = services[name]
        return entry

    registry.get.side_effect = mock_get
    return registry


def test_sync_adds_missing(tmp_path):
    registry = _mock_registry({"svc-a": False, "svc-b": True})
    added, removed = sync_caddy_config(
        registry, "i-a-m.live", conf_dir=tmp_path,
    )
    assert "svc-a" in added
    assert "svc-b" in added
    assert removed == []
    assert (tmp_path / "svc-a.caddy").exists()
    assert (tmp_path / "svc-b.caddy").exists()


def test_sync_removes_stale(tmp_path):
    # Pre-existing stale config
    (tmp_path / "old-svc.caddy").write_text("stale")
    registry = _mock_registry({"new-svc": False})
    added, removed = sync_caddy_config(
        registry, "i-a-m.live", conf_dir=tmp_path,
    )
    assert "new-svc" in added
    assert "old-svc" in removed
    assert not (tmp_path / "old-svc.caddy").exists()


def test_sync_updates_existing(tmp_path):
    (tmp_path / "svc.caddy").write_text("old content")
    registry = _mock_registry({"svc": False})
    added, removed = sync_caddy_config(
        registry, "i-a-m.live", conf_dir=tmp_path,
    )
    # Already existed, so not in "added"
    assert "svc" not in added
    assert removed == []
    # But content updated
    new_content = (tmp_path / "svc.caddy").read_text()
    assert "reverse_proxy" in new_content


def test_sync_domain_and_port_customization(tmp_path):
    registry = _mock_registry({"svc": True})
    sync_caddy_config(
        registry, "custom.dev", gateway_port=9999, caddy_port=8080, conf_dir=tmp_path,
    )
    content = (tmp_path / "svc.caddy").read_text()
    assert "http://svc.custom.dev:8080" in content
    assert "reverse_proxy localhost:9999" in content
