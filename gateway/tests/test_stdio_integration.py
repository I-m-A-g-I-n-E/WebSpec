"""No-mock integration: a real stdio FastMCP server behind the real gateway and ConnectionPool.

Proves that annotations and the ``webspec/tier`` meta survive a real ``tools/list`` and
drive method binding end to end.
"""

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import webspec.app as appmod

SERVER = Path(__file__).parent / "fixtures" / "annotated_server.py"
H = "notes.localhost"


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"notes": {"command": sys.executable, "args": [str(SERVER)]}}}))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(cfg))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "22" * 32)
    monkeypatch.delenv("WEBSPEC_DOMAIN", raising=False)
    with TestClient(appmod.create_app()) as c:  # lifespan closes the stdio child
        yield c


def test_real_server_contracts_drive_method_binding(client):
    listing = client.get("/", headers={"Host": H}).json()
    methods = {t["name"]: t["methods"] for t in listing["tools"]}
    assert methods == {"read_note": ["GET"], "read_secret": ["GET"], "add_note": ["PATCH", "POST"],
                       "wipe": ["DELETE", "PUT"]}

    info = client.options("/read_secret", headers={"Host": H}).json()
    assert info["contract"]["tier"] == "sensitive" and info["contract"]["source"] == "annotations"

    r = client.get("/read_note?id=1", headers={"Host": H})
    assert r.status_code == 200 and r.json()["result"] == "hello"

    r = client.get("/wipe?id=1", headers={"Host": H})
    assert r.status_code == 405 and r.headers["allow"] == "HEAD, OPTIONS, PUT, DELETE"
    assert client.get("/read_note?id=1", headers={"Host": H}).json()["result"] == "hello"  # nothing wiped

    r = client.delete("/wipe?id=1", headers={"Host": H, "X-Gimme-Definer": "REMOVE"})
    assert r.status_code == 200 and r.json()["result"] == "wiped"
    assert client.get("/read_note?id=1", headers={"Host": H}).json()["result"] == ""


def test_index_reports_the_public_port(client, monkeypatch):
    monkeypatch.setenv("WEBSPEC_PORT", "7411")
    services = client.get("/", headers={"Host": "localhost"}).json()["services"]
    assert services == [{"name": "notes", "url": "notes.localhost:7411", "transport": "stdio",
                         "level": 0, "connected": services[0]["connected"]}]


CRASHY = Path(__file__).parent / "fixtures" / "crashy_server.py"


def test_a_crash_mid_call_is_outcome_unknown_and_the_gateway_reconnects(tmp_path, monkeypatch):
    marker = tmp_path / "sent.txt"
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"crashy": {
        "command": sys.executable, "args": [str(CRASHY)], "env": {"CRASHY_MARKER": str(marker)}}}}))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(cfg))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "22" * 32)
    monkeypatch.delenv("WEBSPEC_DOMAIN", raising=False)
    host = "crashy.localhost"
    send = {"Host": host, "X-Gimme-Definer": "SEND", "Idempotency-Key": "crash-1",
            "Content-Type": "application/json"}
    with TestClient(appmod.create_app()) as client:
        r = client.post("/send_and_crash", headers=send, content=b'{"to":"ana"}')
        assert r.status_code == 503 and r.json()["error"] == "service_unavailable"
        assert marker.read_text() == "ana\n"  # it ran before the server died

        r = client.post("/send_and_crash", headers=send, content=b'{"to":"ana"}')
        assert r.status_code == 409 and r.json()["error"] == "idempotency_outcome_unknown"
        assert marker.read_text() == "ana\n"  # and the retry did not run it again

        r = client.get("/alive", headers={"Host": host})
        assert r.status_code == 200 and r.json()["result"] == "yes"  # the gateway reconnected
