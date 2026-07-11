import pytest
from webspec_registry.catalog import harvest


def _fake_fetch(service):
    data = {
        "mail-proton": [{"name": "send_email", "description": "Send an email", "inputSchema": {}}],
        "op-auth": [{"name": "run", "description": "Run op subcommand", "inputSchema": {}}],
    }
    if service == "broken":
        raise RuntimeError("unreachable")
    return data.get(service, [])


def test_harvest_builds_records():
    recs = harvest(["mail-proton", "op-auth"], _fake_fetch)
    by_tool = {r.tool: r for r in recs}
    assert by_tool["send_email"].verb == "send" and by_tool["send_email"].noun == "message"
    assert by_tool["send_email"].tier == "sensitive"
    assert by_tool["run"].tier == "dangerous"


def test_harvest_skips_unreachable_service():
    recs = harvest(["broken", "mail-proton"], _fake_fetch)
    assert {r.service for r in recs} == {"mail-proton"}
