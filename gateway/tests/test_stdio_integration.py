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
