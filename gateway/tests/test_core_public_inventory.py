"""On the public domain, a 404 for an unknown destination names no destinations (HG-7).

Before, ``404 unknown_service`` listed every configured destination, level-0 ones included, to
anyone who asked for a destination that does not exist under the public domain. Loopback
names keep the list. The real app over the fake registry and pool of tests/_gateway.py.
"""

import json

import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

import webspec.app as appmod
from tests._gateway import GUARD_KEY_HEX, FakePool, FakeRegistry, entry, tool

DOMAIN = "example.test"
REQUESTS = [("HEAD", "/"), ("OPTIONS", "/"), ("GET", "/"), ("GET", "/read_note"), ("POST", "/send_note"),
            ("HEAD", "/read_note"), ("OPTIONS", "/read_note"), ("DELETE", "/read_note")]
UNKNOWN = {"error": "unknown_service", "detail": "Unknown service: zzz"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEBSPEC_DOMAIN", DOMAIN)  # before create_app: it adds the public routes
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", GUARD_KEY_HEX)
    app = appmod.create_app()
    appmod.registry = FakeRegistry([entry("notes", level=0), entry("vault", level=1)])
    appmod.pool = FakePool([tool("read_note", read_only=True, open_world=False)])
    return TestClient(app)


def _body(r, method: str) -> dict:
    if method == "HEAD":  # no body; its length still tells whether a list was in it
        return {"content-length": int(r.headers["content-length"])}
    return r.json()


def _expected(method: str, body: dict) -> dict:
    return {"content-length": len(JSONResponse(body).body)} if method == "HEAD" else body


@pytest.mark.parametrize("method, path", REQUESTS)
@pytest.mark.parametrize("host", [f"zzz.{DOMAIN}", f"zzz.{DOMAIN}:443"])
def test_an_unknown_public_destination_names_no_destinations(client, method, path, host):
    r = client.request(method, path, headers={"Host": host, "X-Gimme-Definer": "SEND"})
    assert r.status_code == 404
    assert _body(r, method) == _expected(method, UNKNOWN)


@pytest.mark.parametrize("method, path", REQUESTS)
@pytest.mark.parametrize("host", ["zzz.localhost", "zzz.localhost:7001"])
def test_a_loopback_name_still_gets_the_list(client, method, path, host):
    r = client.request(method, path, headers={"Host": host, "X-Gimme-Definer": "SEND"})
    assert r.status_code == 404
    assert _body(r, method) == _expected(method, {**UNKNOWN, "available": ["notes", "vault"]})


def test_the_public_refusal_is_still_audited(client, tmp_path):
    # AU-1: a request for an unknown destination on the invocation path is a denial.
    r = client.post("/send_note", headers={"Host": f"zzz.{DOMAIN}", "X-Gimme-Definer": "SEND"})
    assert r.status_code == 404
    [line] = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert (line["host"], line["outcome"], line["status"], line["reason"]) == (
        f"zzz.{DOMAIN}", "denied", 404, "unknown_service")


def test_known_destinations_keep_their_public_answers(client):
    # HG-7 is unchanged: a level-0 destination is refused, a guarded one asks for the guard.
    assert client.get("/", headers={"Host": f"notes.{DOMAIN}"}).json()["error"] == "unguarded_public"
    r = client.get("/read_note", headers={"Host": f"vault.{DOMAIN}"})
    assert (r.status_code, r.json()["error"]) == (401, "guard_missing")
