"""A stdio server inherits the gateway's isolation from what its user can write.

The MCP stdio client gives a server only HOME, PATH and a few other variables, plus the
entry's env, and starts it in the gateway's working directory: the gateway's own
PYTHONNOUSERSITE or ``python -I`` never reaches it. A .pth file planted in HOME's user
site-packages would then run in every Python stdio server at its next start, and a module
planted in the working directory (writable by every stdio server on the hardened units)
in every ``python -m`` server. So a gateway that ignores user site-packages (``-I``, as every
shipped production launcher starts it, ``-s`` or PYTHONNOUSERSITE) starts its stdio servers
with PYTHONNOUSERSITE=1, and one that keeps its working directory off sys.path (``-I``, ``-P``
or PYTHONSAFEPATH) starts them in /. A development gateway (no ``-I``, as gateway/systemd and
gateway/launchd start it) starts them as before, user site-packages included.

Real interpreters and real stdio servers behind the real gateway. Documented exception to
"no mocks": in-process tests set the gateway's own mode by replacing the two predicates of
webspec.pool, because sys.flags is fixed for the test process; the subprocess tests read the
real flags.
"""

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from mcp.client.stdio import get_default_environment
from starlette.testclient import TestClient

import webspec.app as appmod
import webspec.pool as pool_mod
from webspec.config import ServiceEntry
from webspec.pool import ConnectionPool
from tests._gateway import FakeRegistry

GATEWAY = Path(__file__).resolve().parent.parent
ENV_SERVER = Path(__file__).parent / "fixtures" / "env_server.py"
ISOLATED, DEVELOPMENT = True, False


@pytest.fixture
def gateway_mode(monkeypatch):
    """Set whether the gateway under test runs isolated (``python -I``) or not."""
    def set_mode(isolated: bool) -> None:
        monkeypatch.setattr(pool_mod, "_ignores_user_site", lambda: isolated)
        monkeypatch.setattr(pool_mod, "_ignores_working_directory", lambda: isolated)
    return set_mode


def _stdio_entry(env) -> ServiceEntry:
    return ServiceEntry(name="svc", original_name="svc", transport_type="stdio", command="python", env=env)


def _transport(env):
    entry = _stdio_entry(env)
    return ConnectionPool(FakeRegistry([entry]))._create_client(entry).transport


def _transport_env(env):
    return _transport(env).env


@pytest.mark.parametrize("env, expected", [
    ({}, {"PYTHONNOUSERSITE": "1"}),
    ({"SOME_TOKEN": "tok"}, {"PYTHONNOUSERSITE": "1", "SOME_TOKEN": "tok"}),
    ({"PYTHONNOUSERSITE": ""}, {"PYTHONNOUSERSITE": ""}),  # the entry's own value wins
    (["x"], ["x"]),  # malformed: passed on to fail at spawn, as before
])
def test_an_isolated_gateway_spawns_stdio_servers_with_pythonnousersite(gateway_mode, env, expected):
    gateway_mode(ISOLATED)
    assert _transport_env(env) == expected


@pytest.mark.parametrize("env, expected", [
    ({}, None),
    ({"SOME_TOKEN": "tok"}, {"SOME_TOKEN": "tok"}),
    ({"PYTHONNOUSERSITE": "1"}, {"PYTHONNOUSERSITE": "1"}),  # an entry can still ask for it
    (["x"], ["x"]),
])
def test_a_development_gateway_spawns_stdio_servers_as_before(gateway_mode, env, expected):
    gateway_mode(DEVELOPMENT)
    assert _transport_env(env) == expected


def _clean_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONNOUSERSITE", "PYTHONSAFEPATH")}
    return {**env, **extra}


@pytest.mark.parametrize("flags, env, expected_env, expected_cwd", [
    (["-I"], {}, {"PYTHONNOUSERSITE": "1"}, "/"),  # how every shipped production launcher starts it
    (["-s"], {}, {"PYTHONNOUSERSITE": "1"}, None),
    ([], {"PYTHONNOUSERSITE": "1"}, {"PYTHONNOUSERSITE": "1"}, None),  # the Docker image's ENV
    (["-P"], {}, None, "/"),
    ([], {"PYTHONSAFEPATH": "1"}, None, "/"),
    ([], {}, None, None),  # how the development units start it
])
def test_the_gateway_reads_its_own_interpreter_flags(flags, env, expected_env, expected_cwd):
    code = (f"import json, sys; sys.path.insert(0, {str(GATEWAY)!r}); "
            "from webspec.pool import stdio_cwd, stdio_env; print(json.dumps([stdio_env({}), stdio_cwd()]))")
    out = subprocess.run([sys.executable, *flags, "-c", code], env=_clean_env(**env), cwd=GATEWAY,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [expected_env, expected_cwd]


@pytest.mark.parametrize("mode, expected", [(ISOLATED, "/"), (DEVELOPMENT, None)])
def test_an_isolated_gateway_starts_stdio_servers_in_the_root_directory(gateway_mode, mode, expected):
    gateway_mode(mode)
    assert _transport({}).cwd == expected


def test_a_python_older_than_3_11_spawns_stdio_servers(monkeypatch):
    """sys.flags.safe_path is new in 3.11, and a development unit runs whatever python3 the host has.

    Reading it unguarded raised AttributeError for every stdio destination when its client was
    created, so each one answered 503 on a 3.10 gateway that ran them before. Python 3.10 run
    with -I: user site-packages ignored, but nothing keeps the working directory off sys.path.
    """
    monkeypatch.setattr(pool_mod, "sys", types.SimpleNamespace(flags=types.SimpleNamespace(no_user_site=1)))
    transport = _transport({"SOME_TOKEN": "tok"})
    assert (transport.env, transport.cwd) == ({"PYTHONNOUSERSITE": "1", "SOME_TOKEN": "tok"}, None)


def _user_site_python() -> str | None:
    """An interpreter that reads HOME's user site-packages (the test venv itself does not)."""
    python = getattr(sys, "_base_executable", None) or sys.executable
    try:
        probe = subprocess.run([python, "-c", "import site; print(site.ENABLE_USER_SITE)"],
                               env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True,
                               timeout=30)
    except OSError:
        return None
    return python if probe.returncode == 0 and probe.stdout.strip() == "True" else None


def _user_site_dir(python: str, home: Path) -> Path:
    site_dir = subprocess.run([python, "-c", "import site; print(site.getusersitepackages())"],
                              env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
                              capture_output=True, text=True, check=True).stdout.strip()
    assert Path(site_dir).is_relative_to(home)
    Path(site_dir).mkdir(parents=True)
    return Path(site_dir)


@pytest.mark.skipif(_user_site_python() is None, reason="no interpreter here reads user site-packages")
def test_code_planted_in_homes_user_site_does_not_run_in_a_stdio_server(tmp_path, gateway_mode):
    python = _user_site_python()
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "planted-code-ran"
    (_user_site_dir(python, home) / "planted.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n")

    def ran(entry_env) -> bool:
        """Start the interpreter with what the MCP client would pass a server from this pool."""
        marker.unlink(missing_ok=True)
        env = {**get_default_environment(), **(_transport_env(entry_env) or {}), "HOME": str(home)}
        subprocess.run([python, "-c", "pass"], env=env, check=True, timeout=30)
        return marker.exists()

    gateway_mode(ISOLATED)
    assert not ran({})
    assert not ran({"SOME_TOKEN": "tok"})
    assert ran({"PYTHONNOUSERSITE": ""})  # an entry can opt back in
    gateway_mode(DEVELOPMENT)
    assert ran({}), "the planted .pth must run without PYTHONNOUSERSITE, or this test proves nothing"


def _serve_config(tmp_path, monkeypatch, servers: dict) -> TestClient:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(path))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "33" * 32)
    monkeypatch.delenv("WEBSPEC_DOMAIN", raising=False)
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    return TestClient(appmod.create_app())


def _getenv(client: TestClient, service: str, name: str):
    r = client.get(f"/getenv?name={name}", headers={"Host": f"{service}.localhost"})
    return r.status_code, (str(r.json().get("result")) if r.status_code == 200 else r.text)  # "1" reads as JSON


@pytest.mark.parametrize("mode, expected", [(ISOLATED, ("1", "")), (DEVELOPMENT, ("<unset>", ""))])
def test_a_real_stdio_server_gets_the_gateways_user_site_policy(tmp_path, monkeypatch, gateway_mode, mode, expected):
    gateway_mode(mode)
    server = {"command": sys.executable, "args": [str(ENV_SERVER)]}
    with _serve_config(tmp_path, monkeypatch, {
        "probe": server,
        "optout": {**server, "env": {"PYTHONNOUSERSITE": ""}},
    }) as client:
        assert (_getenv(client, "probe", "PYTHONNOUSERSITE"), _getenv(client, "optout", "PYTHONNOUSERSITE")) == (
            (200, expected[0]), (200, expected[1]))


@pytest.mark.skipif(_user_site_python() is None, reason="no interpreter here reads user site-packages")
def test_a_development_gateways_stdio_server_keeps_its_user_site_packages(tmp_path, monkeypatch, gateway_mode):
    """The live development host: a server whose packages were installed with pip install --user.

    The server imports a module that exists only in HOME's user site-packages, as
    services/mail-proton or services/op-auth import fastmcp when the system python3 runs them
    and fastmcp was installed with pip install --user. Under a development gateway the server
    still starts; under an isolated one it needs the entry's opt-out. The module is planted
    here, so the test holds wherever fastmcp itself is installed: in a venv behind the
    interpreter, as locally, or in the interpreter's own site-packages, as in CI.
    """
    python = _user_site_python()
    home = tmp_path / "home"
    home.mkdir()
    user_site = _user_site_dir(python, home)
    (user_site / "webspec_userdep.py").write_text('"""Installed with pip install --user, and only there."""\n')
    # An interpreter behind a venv lacks the venv's fastmcp: reach it through user site too.
    deps = Path(importlib.util.find_spec("fastmcp").origin).parent.parent
    (user_site / "deps.pth").write_text(f"{deps}\n")

    def imports(*, with_user_site: bool) -> bool:
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
        if not with_user_site:
            env["PYTHONNOUSERSITE"] = "1"
        return subprocess.run([python, "-c", "import fastmcp, webspec_userdep"], env=env, capture_output=True,
                              timeout=60).returncode == 0
    assert imports(with_user_site=True), "the server's dependencies must import through user site-packages"
    assert not imports(with_user_site=False), "webspec_userdep must import only from there, or this proves nothing"

    server_py = tmp_path / "userdep_server.py"  # env_server, once its user-site dependency is imported
    server_py.write_text(f"import runpy\nimport webspec_userdep\nrunpy.run_path({str(ENV_SERVER)!r}, run_name='__main__')\n")
    monkeypatch.setenv("HOME", str(home))  # the HOME the MCP client passes its servers
    server = {"command": python, "args": [str(server_py)]}
    servers = {"mail": server, "optout": {**server, "env": {"PYTHONNOUSERSITE": ""}}}

    gateway_mode(DEVELOPMENT)
    with _serve_config(tmp_path, monkeypatch, servers) as client:
        assert _getenv(client, "mail", "HOME") == (200, str(home))
    gateway_mode(ISOLATED)
    with _serve_config(tmp_path, monkeypatch, servers) as client:
        assert _getenv(client, "mail", "HOME")[0] == 503  # ModuleNotFoundError: webspec_userdep
        assert _getenv(client, "optout", "HOME") == (200, str(home))


def test_a_module_planted_in_the_working_directory_does_not_run_in_a_stdio_server(tmp_path, monkeypatch,
                                                                                  gateway_mode):
    """``python -m`` puts the working directory first on sys.path. The hardened units run the
    gateway in /var/lib/webspec, which every stdio server can write."""
    workdir = tmp_path / "var-lib-webspec"  # stands in for /var/lib/webspec
    (workdir / "fastmcp").mkdir(parents=True)
    marker = tmp_path / "planted-code-ran"
    (workdir / "fastmcp" / "__init__.py").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "raise ImportError('planted')\n")
    monkeypatch.chdir(workdir)
    server = {"command": sys.executable, "args": ["-m", "env_server"],
              "env": {"PYTHONPATH": str(ENV_SERVER.parent)}}

    gateway_mode(ISOLATED)
    with _serve_config(tmp_path, monkeypatch, {"probe": server}) as client:
        assert _getenv(client, "probe", "PYTHONPATH") == (200, str(ENV_SERVER.parent))
    assert not marker.exists()
    gateway_mode(DEVELOPMENT)
    with _serve_config(tmp_path, monkeypatch, {"probe": server}) as client:
        assert _getenv(client, "probe", "PYTHONPATH")[0] == 503
    assert marker.exists(), "the planted module must run from the working directory, or this test proves nothing"
