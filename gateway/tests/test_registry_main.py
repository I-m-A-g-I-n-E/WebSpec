"""Tests for webspec_registry.__main__: host binding and malformed-index
resilience in service discovery."""
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
