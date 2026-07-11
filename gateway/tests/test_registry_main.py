"""Tests for webspec_registry.__main__: host binding, catalog caching, and
malformed-index resilience in service discovery."""
import json

import webspec_registry.__main__ as registry_main
from webspec_registry.__main__ import _discover_services, _resolve_registry_host


# --- FIX 1: registry must not share the gateway's WEBSPEC_HOST knob ---

def test_resolve_registry_host_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("WEBSPEC_HOST", raising=False)
    monkeypatch.delenv("WEBSPEC_REGISTRY_HOST", raising=False)
    assert _resolve_registry_host() == "127.0.0.1"


def test_resolve_registry_host_ignores_shared_webspec_host(monkeypatch):
    # The gateway sets WEBSPEC_HOST=0.0.0.0 (docker-compose); the registry must not inherit it.
    monkeypatch.setenv("WEBSPEC_HOST", "0.0.0.0")
    monkeypatch.delenv("WEBSPEC_REGISTRY_HOST", raising=False)
    assert _resolve_registry_host() == "127.0.0.1"


def test_resolve_registry_host_honors_distinct_env_var(monkeypatch):
    monkeypatch.setenv("WEBSPEC_HOST", "0.0.0.0")  # present but irrelevant
    monkeypatch.setenv("WEBSPEC_REGISTRY_HOST", "0.0.0.0")
    assert _resolve_registry_host() == "0.0.0.0"


# --- FIX 4: harden discovery against a malformed gateway index ---

class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_discover_services_skips_entries_missing_name(monkeypatch):
    payload = {"services": [
        {"name": "mail-proton"},
        {"notname": "oops"},  # malformed: no "name" key
        {"name": "op-auth"},
        "not-even-a-dict",  # malformed: not a dict at all
    ]}

    def fake_urlopen(req, timeout=5):
        return _FakeResponse(payload)

    monkeypatch.setattr(registry_main.urllib.request, "urlopen", fake_urlopen)
    services = _discover_services("http://localhost:7002")
    assert services == ["mail-proton", "op-auth"]


# --- FIX 3: cache the default catalog so endpoints don't re-harvest every request ---

def test_cached_default_catalog_reuses_result_within_ttl(monkeypatch):
    calls = {"discover": 0, "fetch": 0}

    def fake_discover(gateway_url):
        calls["discover"] += 1
        return ["svc"]

    def fake_http_fetch_tools(gateway_url, guard_key=None):
        def _fetch(service):
            calls["fetch"] += 1
            return [{"name": "do_thing", "description": "", "inputSchema": {}}]
        return _fetch

    monkeypatch.setattr(registry_main, "_discover_services", fake_discover)
    monkeypatch.setattr(registry_main, "http_fetch_tools", fake_http_fetch_tools)
    # Reset module-level cache state so this test is independent of ordering.
    registry_main._cache_value = None
    registry_main._cache_time = None

    first = registry_main._cached_default_catalog()
    second = registry_main._cached_default_catalog()

    assert calls["discover"] == 1
    assert first == second
    assert len(first) == 1


def test_cached_default_catalog_refreshes_after_ttl(monkeypatch):
    calls = {"discover": 0}

    def fake_discover(gateway_url):
        calls["discover"] += 1
        return []

    monkeypatch.setattr(registry_main, "_discover_services", fake_discover)
    monkeypatch.setattr(registry_main, "http_fetch_tools", lambda gateway_url, guard_key=None: (lambda service: []))
    monkeypatch.setattr(registry_main, "CATALOG_TTL", 0)  # expire immediately
    registry_main._cache_value = None
    registry_main._cache_time = None

    registry_main._cached_default_catalog()
    registry_main._cached_default_catalog()

    assert calls["discover"] == 2
