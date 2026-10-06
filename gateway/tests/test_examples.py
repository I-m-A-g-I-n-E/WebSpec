"""The guide's walkthrough comes from a real run; keep that run passing.

docs/guide/walkthrough.md shows what examples/walkthrough.py prints. These tests replay the
same scenarios against the real gateway and the real demo MCP server (stdio, no mocks), so
the guide cannot drift from the implementation. The requests are signed by
examples/shim.py, which is written from the spec text, so they also check spec against code.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import walkthrough  # noqa: E402

from tests._gateway import HAVE_SSH_KEYGEN  # noqa: E402

EXPECTED_STATUSES = {
    0: [200, 405, 200, 200],   # read · GET can't reach a delete · send · delete
    1: [200, 200, 403],        # nonce · signed read · the same request again (nonce reused)
    2: [200, 200, 422],        # send · retry replayed · same key, different request
    3: [403, 200, 200],        # send without clearance · with clearance · read needs none
    4: [200, 428, 200, 428, 200],  # read · send challenged · approved · delete challenged · approved
}


@pytest.mark.parametrize("level", range(5))
def test_walkthrough(level, tmp_path, monkeypatch):
    if level == 4 and not HAVE_SSH_KEYGEN:
        pytest.skip("ssh-keygen not installed")
    for var in ("WEBSPEC_CONFIG", "WEBSPEC_GUARD_KEY", "WEBSPEC_AUDIT_LOG", "WEBSPEC_APPROVERS_FILE"):
        monkeypatch.setenv(var, "")  # scenario() sets these; monkeypatch restores them afterwards
    monkeypatch.delenv("WEBSPEC_DOMAIN", raising=False)
    steps = walkthrough.scenario(level, tmp_path)
    assert [x.status for _, exchanges in steps for x in exchanges] == EXPECTED_STATUSES[level]
