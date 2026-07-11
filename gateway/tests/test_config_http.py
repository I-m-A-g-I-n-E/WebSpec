"""Tests for HTTP service config parsing: headers, env resolution, namespace, guard."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from webspec.config import ServiceEntry, _resolve_env, parse_claude_config


@pytest.fixture
def _config_file():
    """Create a temporary claude.json with HTTP service entries."""
    def _make(servers: dict) -> Path:
        data = {"mcpServers": servers}
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        return Path(f.name)
    yield _make


class TestResolveEnv:
    def test_resolves_known_var(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "abc123")
        assert _resolve_env("Bearer ${MY_TOKEN}") == "Bearer abc123"

    def test_leaves_unknown_var_as_is(self):
        os.environ.pop("NONEXISTENT_VAR_XYZ", None)
        assert _resolve_env("Bearer ${NONEXISTENT_VAR_XYZ}") == "Bearer ${NONEXISTENT_VAR_XYZ}"

    def test_resolves_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert _resolve_env("${A}-${B}") == "1-2"

    def test_no_vars_passthrough(self):
        assert _resolve_env("plain-value") == "plain-value"

    def test_empty_string(self):
        assert _resolve_env("") == ""


class TestHttpServiceParsing:
    def test_http_with_headers(self, _config_file, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret")
        path = _config_file({
            "my-api": {
                "type": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${MY_TOKEN}"},
            }
        })
        registry = parse_claude_config(path)
        entry = registry["my-api"]
        assert entry.transport_type == "http"
        assert entry.url == "https://example.com/mcp"
        assert entry.headers == {"Authorization": "Bearer secret"}

    def test_http_no_headers_defaults_empty(self, _config_file):
        path = _config_file({
            "bare-http": {
                "type": "http",
                "url": "https://example.com/mcp",
            }
        })
        registry = parse_claude_config(path)
        assert registry["bare-http"].headers == {}

    def test_http_guard_flag(self, _config_file):
        path = _config_file({
            "guarded": {
                "type": "http",
                "url": "https://example.com/mcp",
                "guard": True,
            }
        })
        registry = parse_claude_config(path)
        assert registry["guarded"].guard is True

    def test_http_guard_defaults_false(self, _config_file):
        path = _config_file({
            "unguarded": {
                "type": "http",
                "url": "https://example.com/mcp",
            }
        })
        registry = parse_claude_config(path)
        assert registry["unguarded"].guard is False

    def test_namespace_parsed(self, _config_file):
        path = _config_file({
            "ns-svc": {
                "type": "http",
                "url": "https://example.com/mcp",
                "namespace": "user",
            }
        })
        registry = parse_claude_config(path)
        assert registry["ns-svc"].namespace == "user"

    def test_namespace_defaults_none(self, _config_file):
        path = _config_file({
            "no-ns": {
                "type": "http",
                "url": "https://example.com/mcp",
            }
        })
        registry = parse_claude_config(path)
        assert registry["no-ns"].namespace is None

    def test_unresolvable_env_var_no_crash(self, _config_file):
        os.environ.pop("MISSING_VAR_XYZ", None)
        path = _config_file({
            "svc": {
                "type": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${MISSING_VAR_XYZ}"},
            }
        })
        registry = parse_claude_config(path)
        assert registry["svc"].headers == {"Authorization": "Bearer ${MISSING_VAR_XYZ}"}

    def test_stdio_service_unaffected(self, _config_file):
        path = _config_file({
            "my-stdio": {
                "command": "python",
                "args": ["server.py"],
            }
        })
        registry = parse_claude_config(path)
        entry = registry["my-stdio"]
        assert entry.transport_type == "stdio"
        assert entry.headers == {}
        assert entry.namespace is None
