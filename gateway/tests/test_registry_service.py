from starlette.testclient import TestClient
from webspec_registry.app import create_registry_app
from webspec_registry.records import ToolRecord, AccountRecord
from webspec_registry.__main__ import _discover_services

CATALOG = [ToolRecord("slack", "send_message", "Send a message", "send", "message", "sensitive")]
ACCOUNTS = [AccountRecord("Personal", "i1", "Slack", "LOGIN", linked_service="slack")]


def _client():
    app = create_registry_app(catalog_fn=lambda: CATALOG, accounts_fn=lambda: ACCOUNTS)
    return TestClient(app)


def test_resolve_endpoint():
    r = _client().get("/resolve", params={"q": "send a message"})
    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["service"] == "slack"


def test_catalog_endpoint():
    r = _client().get("/catalog")
    assert r.status_code == 200
    assert r.json()["tools"][0]["tool"] == "send_message"


def test_graph_dot_endpoint():
    r = _client().get("/graph", params={"format": "dot"})
    assert r.status_code == 200
    assert r.text.startswith("digraph webspec {")


def test_discover_services_gateway_unreachable():
    """Test that _discover_services returns [] when gateway is unreachable."""
    # Use a closed port to ensure immediate connection failure
    services = _discover_services("http://127.0.0.1:9")
    assert services == []
