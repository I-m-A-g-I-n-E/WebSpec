"""The compose stack is well-formed and the entrypoint sources the guard key from the vault."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # repo root


def test_entrypoint_sources_guard_key():
    text = (ROOT / "docker" / "entrypoint.sh").read_text()
    assert "OP_GUARD_KEY_REF" in text
    assert "op read" in text
    assert "WEBSPEC_GUARD_KEY" in text


def test_compose_declares_three_services():
    text = (ROOT / "docker" / "docker-compose.yml").read_text()
    for svc in ("caddy:", "gateway:", "registry:"):
        assert svc in text


@pytest.mark.skipif(not shutil.which("docker"), reason="docker not installed")
def test_compose_config_valid():
    r = subprocess.run(["docker", "compose", "-f", str(ROOT / "docker" / "docker-compose.yml"), "config"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
