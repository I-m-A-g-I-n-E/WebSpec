"""Each test starts with the PATH the session started with (tests/conftest.py).

webspec-ctl's main() sets PATH to SAFE_PATH when it runs as root (DP-1, DP-4), which is right
for its own process. Tests call it in-process, so in a root run every later test inherited
SAFE_PATH, and results depended on test order: a test that needs python3 from the session's
PATH failed after test_ctl.py and passed alone. The two tests below run in this order; the
second fails without the conftest's PATH reset.
"""

import os

import pytest

from webspec import ctl
from webspec.caddy import SAFE_PATH

SESSION_PATH = os.environ.get("PATH", os.defpath)  # read at collection, before any test ran


def test_ctl_main_as_root_sets_the_safe_path(monkeypatch):
    if SESSION_PATH == SAFE_PATH:
        pytest.skip("the session's PATH is SAFE_PATH: no change to tell apart")
    monkeypatch.setattr(ctl.os, "geteuid", lambda: 0)
    ctl.main(["ls"])  # the config does not exist: it changes PATH, prints an error, does nothing
    assert os.environ["PATH"] == SAFE_PATH  # left in place, as main() leaves it for its process


def test_the_next_test_starts_with_the_sessions_path():
    assert os.environ.get("PATH", os.defpath) == SESSION_PATH
