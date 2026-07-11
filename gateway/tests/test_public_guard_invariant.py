"""An unguarded service must never be reachable on the public domain."""
import pytest
from starlette.testclient import TestClient

import webspec.app as appmod
from webspec.config import ServiceEntry


class _FakeRegistry:
    def __init__(self, entry): self._entry = entry
    def get(self, name): return self._entry if name == "mail-proton" else None
    def names(self): return ["mail-proton"]
    @property
    def services(self): return {"mail-proton": self._entry}


@pytest.fixture
def public_client(monkeypatch):
    monkeypatch.setenv("WEBSPEC_DOMAIN", "i-a-m.live")
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    entry = ServiceEntry(name="mail-proton", original_name="mail-proton",
                         transport_type="stdio", command="x", guard=False)
    appmod.registry = _FakeRegistry(entry)
    app = appmod.create_app()
    appmod.registry = _FakeRegistry(entry)  # create_app resets it; pin our fake
    return TestClient(app)


def test_unguarded_service_blocked_on_public_domain(public_client):
    r = public_client.get("/", headers={"Host": "mail-proton.i-a-m.live"})
    assert r.status_code == 403
    assert r.json()["error"] == "unguarded_public"


def test_unguarded_service_allowed_on_localhost(public_client):
    # localhost is not the public domain → normal handling (503/200, not 403)
    r = public_client.get("/", headers={"Host": "mail-proton.localhost"})
    assert r.status_code != 403
