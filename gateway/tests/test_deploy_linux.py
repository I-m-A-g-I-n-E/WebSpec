"""The Linux production deployment (gateway/deploy/linux) against DP-1 to DP-9 and GD-5.

install.sh runs with --dry-run, which needs no root and changes nothing: it prints the plan
for a fresh host, and these tests pin that plan. A real run as a user other than root must
refuse before it plans anything. Sourcing the script defines its functions without running
anything, so the functions of a real run are run on their own: the checks that need no root
(who can change an interpreter, a venv or pip's configuration, whether a venv can be reused,
whether a unit file is the installer's), and, with stand-ins for systemctl, ss, getent and
the rest that record every call, the parts that act (holding the port, starting and
verifying the gateway, the account, a foreign unit, drop-ins, the venv's rollback). The
systemd units are parsed and their directives checked. All of it runs on macOS (bash 3.2)
and on Linux CI. A real install of these files, following the guide, was verified by hand in
privileged Debian 13 systemd containers, as the pull request that added them describes.
"""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from webspec import approval
from webspec.config import parse_claude_config

GATEWAY = Path(__file__).resolve().parents[1]
LINUX = GATEWAY / "deploy" / "linux"
INSTALL = LINUX / "install.sh"
UNIT = LINUX / "webspec-gateway.service"
SOCKET = LINUX / "webspec-gateway.socket"
DEV_UNIT = GATEWAY / "systemd" / "webspec-gateway.service"
EXAMPLE = GATEWAY / "deploy" / "config.example.json"
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")


def installer_env(**env: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items()
            if k not in ("DRY_RUN", "PYTHON", "ALLOW_NONROOT_PYTHON", "ALLOW_EXISTING_USER")}
    return {**base, **env}


def run_installer(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    """install.sh from an unrelated directory (it must find the repository by itself)."""
    return subprocess.run([BASH, str(INSTALL)], env=installer_env(**env), cwd=tmp_path,
                          capture_output=True, encoding="utf-8", timeout=60)


def sourced(script: str, **env: str) -> subprocess.CompletedProcess:
    """Runs script in a bash that has sourced install.sh (functions only, nothing runs)."""
    return subprocess.run([BASH, "-c", 'source "$1" && ' + script, "test", str(INSTALL)],
                          env=installer_env(**env), capture_output=True, encoding="utf-8", timeout=60)


def run_options(argv: list[str], **env: str) -> subprocess.CompletedProcess:
    """install.sh ARGV from an unrelated directory."""
    return subprocess.run([BASH, str(INSTALL), *argv], env=installer_env(**env), cwd="/",
                          capture_output=True, encoding="utf-8", timeout=60)


def python3_on_path() -> str:
    """What a sourced install.sh resolves python3 to: the physical path of python3 on PATH."""
    found = shutil.which("python3")
    return os.path.realpath(found) if found else "python3"


ROOT_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"  # what install.sh starts over with as root (F14)


def planned_python3() -> str:
    """What the dry run resolves python3 to. As root, install.sh starts over with ROOT_PATH and
    looks there, and plans with plain python3 if there is none (P17: python:3.12-slim has
    python3 in /usr/local/bin only); otherwise it looks on the caller's PATH."""
    if os.geteuid() != 0:
        return python3_on_path()
    found = shutil.which("python3", path=ROOT_PATH)
    return os.path.realpath(found) if found else "python3"


NOBODY = 65534


def give_away(*paths: Path) -> None:
    """As root, every file a test makes is root's, so a check for files that a user other than
    root can change finds none (P17). Hand them to nobody's UID then, so that the check still
    has something to find. Does nothing for anyone else, whose files already count."""
    if os.geteuid() != 0:
        return
    for path in paths:
        try:
            os.lchown(path, NOBODY, NOBODY)
        except OSError as exc:  # a user namespace without that UID
            pytest.skip(f"cannot give {path} to UID {NOBODY}: {exc}")


@pytest.fixture(scope="module")
def plan(tmp_path_factory) -> str:
    if BASH is None:
        pytest.skip("bash not installed")
    r = run_installer(tmp_path_factory.mktemp("cwd"), DRY_RUN="1")
    assert r.returncode == 0, r.stderr
    return r.stdout


def commands(plan: str) -> list[list[str]]:
    """The commands of the plan: its '+ ' lines, parsed as the shell would."""
    out = []
    for line in plan.splitlines():
        if line.startswith("+ "):
            argv = shlex.split(line[2:])
            out.append(argv[:-1] if argv[-1] == "<<EOF" else argv)
    return out


def written(plan: str, dest: str) -> str:
    """The content the plan writes to dest from a here-document."""
    lines = plan.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("+ install ") and ln.endswith(f" {dest} <<EOF"))
    end = lines.index("EOF", start)
    return "\n".join(lines[start + 1:end]) + "\n"


def position(cmds: list[list[str]], argv: list[str]) -> int:
    assert argv in cmds, f"{shlex.join(argv)} is not in the plan"
    return cmds.index(argv)


def next_steps(plan: str) -> str:
    return plan.split("\nNext steps:\n", 1)[1]


# ── install.sh ───────────────────────────────────────────────────────────────


@needs_bash
def test_dry_run_says_so(plan):
    # The fixture ran it as whoever runs the tests, normally not root.
    assert plan.startswith("DRY RUN:")


@needs_bash
@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="must never run the real installer as root here")
@pytest.mark.parametrize("dry_run", [None, "0", ""])
def test_a_real_run_refuses_without_root(tmp_path, dry_run):
    env = {} if dry_run is None else {"DRY_RUN": dry_run}
    r = run_installer(tmp_path, **env)
    assert r.returncode != 0
    assert "run as root" in r.stderr
    assert "+ " not in r.stdout  # nothing was planned, let alone run


@needs_bash
@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="must never run the real installer as root here")
def test_a_real_run_refuses_a_checkout_that_others_can_change(tmp_path):
    """DP-1/DP-4: root builds, installs and starts the gateway from the checkout, so one the
    agent can write (its working tree, say) is refused before anything is planned. This
    copy is owned by whoever runs the tests, not by root."""
    checkout = tmp_path / "WebSpec"
    for rel in ("pyproject.toml", "webspec/__main__.py", "deploy/config.example.json",
                "deploy/linux/webspec-gateway.service", "deploy/linux/webspec-gateway.socket",
                "deploy/linux/install.sh"):
        (checkout / "gateway" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(GATEWAY / rel, checkout / "gateway" / rel)
    r = subprocess.run([BASH, str(checkout / "gateway" / "deploy" / "linux" / "install.sh")], env=installer_env(),
                       cwd=tmp_path, capture_output=True, encoding="utf-8", timeout=60)
    assert r.returncode != 0
    real = os.path.realpath(checkout)  # the script resolves symlinks (/tmp is one on macOS)
    assert f"refusing to install from {real}: users other than root can change it" in r.stderr
    assert real in r.stderr.split("by others:", 1)[1]  # the offending paths are listed
    assert "sudo git clone" in r.stderr  # and how to get a checkout only root can change
    assert "run as root" in r.stderr  # both problems are reported at once
    assert "+ " not in r.stdout


@needs_bash
def test_plan_checks_the_checkout_before_anything_else(plan):
    checkout = os.path.realpath(GATEWAY.parent)
    check = (f"# check: only root can change {checkout} or the directories above it "
             "(root runs this script and installs the gateway from it)")
    lines = plan.splitlines()
    assert check in lines
    assert lines.index(check) < next(i for i, ln in enumerate(lines) if ln.startswith("+ "))


@needs_bash
@pytest.mark.parametrize("value", ["1", "yes", "true"])
def test_any_other_dry_run_value_is_a_dry_run(tmp_path, value):
    r = run_installer(tmp_path, DRY_RUN=value)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("DRY RUN:")


@needs_bash
def test_unknown_arguments_are_refused(tmp_path):
    r = subprocess.run([BASH, str(INSTALL), "--frobnicate"], env=installer_env(DRY_RUN="1"), cwd=tmp_path,
                       capture_output=True, encoding="utf-8", timeout=60)
    assert r.returncode != 0 and "unknown argument: --frobnicate" in r.stderr
    r = subprocess.run([BASH, str(INSTALL), "--help"], env=installer_env(), cwd=tmp_path,
                       capture_output=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0 and "Install the WebSpec gateway" in r.stdout
    assert not [ln for ln in r.stdout.splitlines() if ln.startswith("+ ")]  # the help, not a plan


@needs_bash
def test_plan_creates_the_dedicated_service_user(plan):
    """DP-1: a system user with no login shell, its home the state directory."""
    cmds = commands(plan)
    assert ["groupadd", "--system", "webspec"] in cmds
    useradd = next(c for c in cmds if c[0] == "useradd")
    assert "--system" in useradd and "--no-create-home" in useradd and useradd[-1] == "webspec"
    opts = dict(zip(useradd, useradd[1:]))
    assert opts["--gid"] == "webspec"
    assert opts["--home-dir"] == "/var/lib/webspec"
    assert opts["--shell"] == "/usr/sbin/nologin"
    assert "# if group webspec does not exist:" in plan and "# if user webspec does not exist:" in plan


@needs_bash
@pytest.mark.parametrize("argv", [
    # state directory: HOME and the audit log, closed to everyone else (DP-4)
    ["install", "-d", "-m", "0700", "-o", "webspec", "-g", "webspec", "/var/lib/webspec"],
    # code: root-owned, so the service user cannot change what it runs
    ["install", "-d", "-m", "0755", "-o", "root", "-g", "root", "/opt/webspec"],
    ["chown", "-R", "root:root", "/opt/webspec/venv"],
    ["chmod", "-R", "go-w", "/opt/webspec/venv"],
    ["chmod", "0755", "/opt/webspec/venv"],
    # configuration: root-owned, readable (not writable) by the service (DP-4)
    ["install", "-d", "-m", "0750", "-o", "root", "-g", "webspec", "/etc/webspec"],
    ["install", "-m", "0640", "-o", "root", "-g", "webspec", "/dev/stdin", "/etc/webspec/config.json"],
    ["chown", "root:webspec", "/etc/webspec/config.json"],
    ["chmod", "0640", "/etc/webspec/config.json"],
    ["install", "-m", "0644", "-o", "root", "-g", "root", str(EXAMPLE), "/etc/webspec/config.example.json"],
    # secrets: readable by root only; systemd loads them before dropping privileges (GD-5)
    ["install", "-m", "0600", "-o", "root", "-g", "root", "/dev/stdin", "/etc/webspec/gateway.env"],
    ["chown", "root:root", "/etc/webspec/gateway.env"],
    ["chmod", "0600", "/etc/webspec/gateway.env"],
    # approvers: public keys only
    ["install", "-m", "0644", "-o", "root", "-g", "root", "/dev/stdin", "/etc/webspec/allowed_signers"],
    ["chown", "root:root", "/etc/webspec/allowed_signers"],
    ["chmod", "0644", "/etc/webspec/allowed_signers"],
    # the system units
    ["install", "-m", "0644", "-o", "root", "-g", "root", str(SOCKET), "/etc/systemd/system/webspec-gateway.socket"],
    ["install", "-m", "0644", "-o", "root", "-g", "root", str(UNIT), "/etc/systemd/system/webspec-gateway.service"],
])
def test_plan_sets_exact_owners_and_modes(plan, argv):
    position(commands(plan), argv)


@needs_bash
def test_plan_builds_the_venv_from_this_checkout(plan):
    cmds = commands(plan)
    venv = position(cmds, [planned_python3(), "-I", "-S", "-m", "venv", "/opt/webspec/venv"])
    pip = next(i for i, c in enumerate(cmds) if c[:5] == ["/opt/webspec/venv/bin/python", "-I", "-m", "pip", "--isolated"])
    assert cmds[pip][-1] == str(GATEWAY)
    assert venv < pip < position(cmds, ["chown", "-R", "root:root", "/opt/webspec/venv"])
    assert ("# if /opt/webspec/venv does not exist, holds a file a user other than root can change "
            "(moved aside, never run), or cannot be reused (another interpreter or Python X.Y, no working "
            "pip), a new one is built in its place; the old one waits aside until the new one has its "
            "packages and imports, and is put back if it does not:") in plan


@needs_bash
def test_the_installed_code_is_checked_as_the_service_user(plan):
    cmds = commands(plan)
    check = next(c for c in cmds if c[0] == "runuser")
    assert check[:4] == ["runuser", "-u", "webspec", "--"]
    assert check[4:7] == ["/usr/bin/env", "-i", "HOME=/var/lib/webspec"]
    assert check[-4:] == ["/opt/webspec/venv/bin/python", "-I", "-c", "import webspec.app"]
    assert position(cmds, ["chmod", "0755", "/opt/webspec/venv"]) < cmds.index(check)


def python_calls(cmds: list[list[str]]) -> list[list[str]]:
    """Each Python invocation of the plan, from the interpreter on (runuser and env aside)."""
    out = []
    for c in cmds:
        at = next((i for i, a in enumerate(c) if os.path.basename(a).startswith("python")), None)
        if at is not None:
            out.append(c[at:])
    return out


@needs_bash
def test_root_never_imports_from_its_working_directory(plan):
    """Python puts the working directory first on sys.path, and root's may be writable by the
    agent (/tmp, a shared home): every Python runs isolated (-I), and the script works from /."""
    pythons = python_calls(commands(plan))
    assert len(pythons) == 3  # venv, pip and the import check
    assert all(c[1] == "-I" for c in pythons), pythons
    # Wherever the script runs an interpreter with options, the first one is -I.
    lines = INSTALL.read_text(encoding="utf-8").splitlines()
    calls = []
    for i, ln in enumerate(lines):
        for m in re.finditer(r'"\$(PYTHON|PY_REAL|VENV/bin/python|1/bin/python)"\s+(-\S*)', ln):
            assert m.group(2) == "-I", ln
            calls.append(i)
    assert len(calls) >= 6, calls  # the version checks, the venv probe, venv, pip, the import check
    assert lines.index("cd /") < min(calls)


def function_body(name: str) -> str:
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index(f"\n{name}() {{\n")
    return text[start:text.index("\n}\n", start)]


@needs_bash
def test_the_base_interpreter_runs_without_site_processing(plan):
    """P6: the interpreter that builds the venv runs with -I -S wherever root runs it: the
    version checks, the queries for ensurepip's wheels and the site directories, -m venv. Its
    site directories are not the venv's, and a .pth file in one runs code (Debian puts
    /usr/local/lib/python3.X/dist-packages there)."""
    venv = next(c for c in python_calls(commands(plan)) if c[-3:] == ["-m", "venv", "/opt/webspec/venv"])
    assert venv[1:3] == ["-I", "-S"]
    text = INSTALL.read_text(encoding="utf-8")
    calls = re.findall(r'"\$PY_REAL" (-\S+(?: -\S+)*)', text)
    assert len(calls) >= 4, calls  # need, PY_VERSION, -m venv, and the dry run's -m venv
    assert all(opts.startswith("-I -S") for opts in calls), calls
    for name in ("python_unsafe", "site_unsafe"):  # the queries
        assert re.findall(r'"\$1" (-\S+ -\S+)', function_body(name)) == ["-I -S"], name


@needs_bash
def test_root_starts_over_in_a_clean_environment():
    """F14 (DP-1, DP-4): as root, before anything else runs, the script re-executes itself in an
    empty environment with a fixed PATH and HOME whenever the environment holds any variable but
    DRY_RUN and the proxy variables. Nothing of the invoking user's (sudo -E, env_keep) reaches
    pip or the PATH lookups, and nothing names the interpreter root runs: PYTHON and the ALLOW_*
    switches are options now, which no environment can supply."""
    text = INSTALL.read_text(encoding="utf-8")
    block = text[text.index("if ((EUID == 0)) && [["):text.index('/bin/bash "$0" "$@"')]
    allowed = block.split("case \"$_name\" in", 1)[1].split(") ;;", 1)[0]
    names = {n.strip() for n in allowed.replace("\\\n", " ").split("|")}
    assert names == {"PATH", "HOME", "PWD", "OLDPWD", "SHLVL", "_", "DRY_RUN", "WEBSPEC_INSTALL_CLEAN",
                     "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
    argv = shlex.split(block.split("exec ", 1)[1].replace("\\\n", " "))
    assert argv[:5] == ["/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/root",
                        "WEBSPEC_INSTALL_CLEAN=1"]
    kept = {re.match(r"\$\{(\w+)\+", a).group(1) for a in argv[5:]}
    assert kept == {"DRY_RUN", "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
    # A clean environment that still names the installer's variables is forced to the fixed values.
    assert "PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root\n  export PATH HOME" in text
    lines = text.splitlines()
    assert lines.index("set -euo pipefail") < next(i for i, ln in enumerate(lines) if ln.startswith("if ((EUID == 0))")) < \
        lines.index("umask 022 # the venv must be readable by the service user, whatever root's umask is")
    # pip reads the global configuration, which must be root's (pip_config_unsafe), and nothing else.
    assert "PIP_CONFIG_FILE" not in text
    assert text.startswith("#!/bin/bash\n")  # not bash from a PATH lookup
    assert "never sudo -E" in text.split("set -euo pipefail")[0]


@needs_bash
@pytest.mark.parametrize("name", ["PYTHON", "ALLOW_NONROOT_PYTHON", "ALLOW_EXISTING_USER"])
def test_the_environment_no_longer_chooses_the_overrides(tmp_path, name):
    """F14: under sudo -E these would come from the invoking user's environment, which may be
    the agent's. They are ignored, and said so; the options take their place."""
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\ntouch \"$0.ran\"\nexit 1\n")
    fake.chmod(0o755)
    value = str(fake) if name == "PYTHON" else "1"
    r = run_options(["--dry-run"], **{name: value})
    assert r.returncode == 0, r.stderr
    assert f"install.sh: ignoring {name} from the environment: the overrides are options (see --help)" in r.stderr
    assert str(fake) not in r.stdout and "Going ahead because of --allow-nonroot-python" not in r.stderr
    assert [planned_python3(), "-I", "-S", "-m", "venv", "/opt/webspec/venv"] in commands(r.stdout)
    assert not (tmp_path / "python3.ran").exists()


@needs_bash
def test_options():
    r = sourced('parse_args -n --python=/usr/bin/python3.12 --allow-nonroot-python --allow-existing-user '
                '--allow-drop-ins && echo "$DRY $OPT_PYTHON $OPT_ALLOW_NONROOT_PYTHON $OPT_ALLOW_EXISTING_USER '
                '$OPT_ALLOW_DROP_INS"', DRY_RUN="0")
    assert r.returncode == 0 and r.stdout == "1 /usr/bin/python3.12 1 1 1\n", r.stderr
    r = sourced('parse_args --python /opt/py/bin/python3 && echo "$DRY $OPT_PYTHON $OPT_ALLOW_NONROOT_PYTHON"',
                DRY_RUN="0")
    assert r.stdout == "0 /opt/py/bin/python3 0\n", r.stderr
    for bad in ("--python", "--python=", "--allow-everything"):
        r = sourced(f"parse_args {bad}")
        assert r.returncode == 1 and r.stderr.startswith("install.sh: "), (bad, r.stderr)


@needs_bash
def test_a_named_python_is_used(tmp_path):
    r = run_options(["--dry-run", "--python=/nonexistent/python3.12"])
    assert r.returncode == 0, r.stderr
    assert ["/nonexistent/python3.12", "-I", "-S", "-m", "venv", "/opt/webspec/venv"] in commands(r.stdout)


@needs_bash
def test_pip_runs_isolated(plan):
    pip = next(c for c in commands(plan) if c[1:5] == ["-I", "-m", "pip", "--isolated"])
    assert "--index-url" not in pip and "-i" not in pip[5:]


@needs_bash
def test_plan_never_overwrites_existing_configuration(plan):
    for path in ("/etc/webspec/config.json", "/etc/webspec/gateway.env", "/etc/webspec/allowed_signers"):
        assert f"# if {path} does not exist:" in plan


@needs_bash
def test_a_new_configuration_has_no_servers(plan):
    """F16: config.json starts with no live servers; the example is a reference beside it."""
    text = written(plan, "/etc/webspec/config.json")
    assert json.loads(text)["mcpServers"] == {}
    assert "config.example.json" in text and "sudo -e" in text  # where the format is, and how not to edit


@needs_bash
def test_the_new_configuration_registers_nothing(plan, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(written(plan, "/etc/webspec/config.json"))
    assert parse_claude_config(config) == {}


def test_the_example_runs_files_a_checkout_has():
    """F16: the example's stdio server is the demo server of a checkout in /opt/webspec/src,
    where the installers suggest cloning one, not a path that nothing installs."""
    servers = json.loads(EXAMPLE.read_text(encoding="utf-8"))["mcpServers"]
    for entry in servers.values():
        for arg in entry.get("args", []):
            assert arg.startswith("/opt/webspec/src/"), arg
            assert (GATEWAY.parent / arg.removeprefix("/opt/webspec/src/")).is_file(), arg
    assert "sudo git clone https://github.com/I-m-A-g-I-n-E/WebSpec.git /opt/webspec/src" in INSTALL.read_text()


@needs_bash
def test_plan_holds_the_port_on_every_run_and_enables_the_gateway_only_with_its_key(plan):
    """F2, F17: the socket is installed, enabled and started on every run, key or no key, so
    systemd holds 127.0.0.1:7002 from the first run on (an existing Caddy may forward there).
    The gateway itself is enabled and started only behind the guard-key check."""
    cmds = commands(plan)
    socket = position(cmds, ["install", "-m", "0644", "-o", "root", "-g", "root", str(SOCKET),
                             "/etc/systemd/system/webspec-gateway.socket"])
    service = position(cmds, ["install", "-m", "0644", "-o", "root", "-g", "root", str(UNIT),
                              "/etc/systemd/system/webspec-gateway.service"])
    reloaded = position(cmds, ["systemctl", "daemon-reload"])
    socket_enabled = position(cmds, ["systemctl", "enable", "--quiet", "webspec-gateway.socket"])
    service_enabled = position(cmds, ["systemctl", "enable", "--quiet", "webspec-gateway.service"])
    assert socket < service < reloaded < socket_enabled < service_enabled
    assert [c for c in cmds if c[:2] == ["systemctl", "enable"]] == [cmds[socket_enabled], cmds[service_enabled]]
    lines = plan.splitlines()
    held = lines.index("# if no user-level webspec-gateway.service is running (it binds the port itself), "
                       "with or without the guard key:")
    keyed = lines.index("# if /etc/webspec/gateway.env sets WEBSPEC_GUARD_KEY to more than whitespace and "
                        "invisible characters:")
    assert lines.index("+ systemctl daemon-reload") < held < lines.index(
        "+ systemctl enable --quiet webspec-gateway.socket") < keyed < lines.index(
        "+ systemctl enable --quiet webspec-gateway.service")
    # F12: drop-ins outlive the unit files, so the units are checked as systemd will run them.
    effective = next(i for i, ln in enumerate(lines) if ln.startswith(
        "# check: the units as systemd runs them, drop-ins included, run /opt/webspec/venv/bin/python -I -m "
        "webspec as webspec after the guard-key check, read only /etc/webspec/gateway.env and listen on "
        "127.0.0.1:7002 only; no drop-ins in /etc or /run (or --allow-drop-ins)"))
    assert lines.index("+ systemctl daemon-reload") < effective < held


@needs_bash
def test_plan_binds_the_port_anew_only_when_it_must_and_stops_the_tunnel_meanwhile(plan):
    """F2: binding the port anew frees it for a moment, so it happens only for a new socket, new
    directives or a port systemd does not hold, with cloudflared stopped meanwhile. A re-run
    restarts the gateway while the socket keeps the port bound."""
    cmds = commands(plan)
    order = [position(cmds, c) for c in (
        ["systemctl", "enable", "--quiet", "webspec-gateway.socket"],
        ["systemctl", "stop", "cloudflared.service"],
        ["systemctl", "stop", "webspec-gateway.service", "webspec-gateway.socket"],
        ["systemctl", "start", "webspec-gateway.socket"],
        ["systemctl", "start", "cloudflared.service"],
        ["systemctl", "enable", "--quiet", "webspec-gateway.service"],
        ["systemctl", "restart", "webspec-gateway.service"],
    )]
    assert order == sorted(order)
    assert ["systemctl", "start", "webspec-gateway.service"] not in cmds  # restart: a re-run loads the new code
    assert ("#   if webspec-gateway.socket is new, its directives changed, or systemd does not hold "
            "127.0.0.1:7002 (the port is free for a moment):") in plan
    assert "#     if something listens on the port now and cloudflared.service is running:" in plan
    assert "runs as webspec and answers on 127.0.0.1:7002, held by systemd" in plan


@needs_bash
def test_next_steps_enable_and_start_the_gateway(plan):
    steps = next_steps(plan)
    assert "sudo systemctl enable --now webspec-gateway.service" in steps
    assert "webspec-gateway.socket holds 127.0.0.1:7002 already" in steps
    assert "sudo systemctl restart webspec-gateway.service" in steps  # restart, not start (F12)
    assert not re.search(r"systemctl start\b", steps)


@needs_bash
def test_next_steps_never_hand_gateway_env_to_the_invoking_user(plan):
    """F4 (DP-1, GD-5): sudoedit copies gateway.env, guard key included, to a file of the
    invoking user, who may be the agent's. The installer says how to edit the files as root and
    warns against sudo -e; nothing recommends it."""
    steps = next_steps(plan)
    assert "sudo -H /usr/bin/vi /etc/webspec/gateway.env" in steps
    assert "| sudo /usr/bin/tee -a /etc/webspec/gateway.env" in steps
    assert "Never with sudo -e (sudoedit)" in steps
    text = INSTALL.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "sudoedit" in line or "sudo -e" in line:
            assert re.search(r"[Nn]ever with sudo -e", line), line


@needs_bash
def test_next_steps_put_caddy_in_front_and_leave_egress_to_the_operator(plan):
    """F19: the Caddy step is setup-caddy.sh, which holds both loopback addresses; DP-3 is the
    operator's and is said so."""
    steps = next_steps(plan)
    assert f"sudo {GATEWAY}/tools/setup-caddy.sh" in steps
    assert (GATEWAY / "tools" / "setup-caddy.sh").is_file()
    assert "127.0.0.1:7001 and [::1]:7001, sockets held by systemd" in steps
    assert "DP-3 is not set up here" in steps
    assert f"see the deployment guide,\n     {GUIDE}\n" in steps  # named, not just mentioned


GUIDE = "https://i-m-a-g-i-n-e.github.io/WebSpec/guide/deploy/"


@pytest.mark.parametrize("path", [UNIT, SOCKET])
def test_the_units_document_the_deployment_guide(path):
    assert parse_unit(path.read_text(encoding="utf-8"))["Unit"]["Documentation"] == [GUIDE]


def test_the_header_names_the_rules_the_script_covers():
    """F19: it sets up DP-1, DP-2, DP-4, DP-6, DP-7, DP-8, DP-9 and GD-5; DP-5 is setup-caddy.sh's and
    DP-3 the operator's."""
    header = INSTALL.read_text(encoding="utf-8").split("set -euo pipefail")[0]
    assert "DP-1 to DP-8" not in header
    assert "DP-1, DP-2, DP-4, DP-6, DP-7, DP-8 and DP-9" in header and "GD-5" in header
    assert "setup-caddy.sh" in header and "(DP-3)" in header


@needs_bash
def test_plan_checks_the_account_the_interpreter_and_an_existing_gateway(plan):
    lines = plan.splitlines()
    first_change = next(i for i, ln in enumerate(lines) if ln.startswith("+ "))
    checks = [
        "# check: no symlinks at /opt/webspec, /opt/webspec/venv, /etc/webspec and the files in it",  # F5
        f"# check: only root can change {planned_python3()}, the directories above it",  # F6
        "# check: an existing user webspec is a system account with no password and no login shell",  # F15
        "# check: no webspec-gateway.service that this installer did not write",  # F12
    ]
    for check in checks:
        at = next((i for i, ln in enumerate(lines) if ln.startswith(check)), None)
        assert at is not None and at < first_change, check
    # P6: the interpreter's site directories, which nothing here reads, get a warning at most.
    assert f"# check (warning only): only root can change the site directories that {planned_python3()}" in plan


@needs_bash
def test_plan_never_touches_a_user_unit(plan):
    assert "# check: warn when a user-level webspec-gateway.service is running" in plan
    assert not any("--user" in c for c in commands(plan))


@needs_bash
def test_no_load_credential(plan):
    # Its credential file would be readable by the stdio servers, which run as the same user.
    assert "LoadCredential" not in plan
    assert not set(parse_unit(UNIT.read_text(encoding="utf-8"))["Service"]) & {
        "LoadCredential", "LoadCredentialEncrypted", "SetCredential", "SetCredentialEncrypted", "ImportCredential"}


@needs_bash
def test_gateway_env_template_holds_no_secret_and_an_empty_guard_key(plan):
    text = written(plan, "/etc/webspec/gateway.env")
    assignments = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert assignments == ["WEBSPEC_DOMAIN=", "WEBSPEC_GUARD_KEY="]
    assert "op read" in text  # how to fill it from a password manager
    assert "Never with sudo -e (sudoedit)" in text and "sudo -H /usr/bin/vi /etc/webspec/gateway.env" in text


@needs_bash
def test_allowed_signers_template_names_nobody(plan, tmp_path, monkeypatch):
    """AP-7: until an approver is added, level-4 requests get 503, never an unsignable 428."""
    signers = tmp_path / "allowed_signers"
    signers.write_text(written(plan, "/etc/webspec/allowed_signers"))
    monkeypatch.setenv("WEBSPEC_APPROVERS_FILE", str(signers))
    monkeypatch.setenv("WEBSPEC_SSH_KEYGEN", sys.executable)  # any executable: only the file is in question
    assert not approval.approvers_available()
    signers.write_text(signers.read_text() + 'ana@example.com namespaces="webspec-approval" ssh-ed25519 AAAA\n')
    assert approval.approvers_available()


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_install_script_is_shellcheck_clean():
    r = subprocess.run(["shellcheck", str(INSTALL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_install_script_is_executable():
    assert os.access(INSTALL, os.X_OK)


# ── install.sh, its checks on their own (sourced) ────────────────────────────


@needs_bash
def test_sourcing_runs_nothing(tmp_path):
    r = sourced("echo sourced", DRY_RUN="0")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "sourced\n" and r.stderr == ""


@needs_bash
def test_an_interpreter_another_user_can_change_is_unsafe(tmp_path):
    """F6 (DP-1): the venv runs the base interpreter and its standard library in place, as the
    gateway's user. One that a user other than root can change is caught before it runs."""
    prefix = tmp_path / "py"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib" / "python3.12" / "encodings").mkdir(parents=True)
    exe = prefix / "bin" / "python3.12"
    exe.write_text("#!/bin/sh\necho executed > \"$(dirname \"$0\")/ran\"\n")
    exe.chmod(0o755)
    give_away(exe)
    r = sourced(f'python_unsafe "$(readlink -f {shlex.quote(str(exe))})"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == os.path.realpath(exe)  # the first thing others can change
    assert not (prefix / "bin" / "ran").exists()  # and it was never run


def root_alone(path: str) -> bool:
    """Roughly what python_unsafe asks of path: that only root can change it, the directories
    above it and, for a directory, everything in it, wherever its symbolic links lead."""
    searched: set[str] = set()

    def node(p: str) -> bool:
        st = os.lstat(p)
        return st.st_uid == 0 and (stat.S_ISLNK(st.st_mode) or not st.st_mode & 0o022)

    def chain(p: str) -> bool:
        return all(node(str(q)) for q in [Path(p), *Path(p).parents])

    def tree(top: str) -> bool:
        real = os.path.realpath(top)
        if real in searched:
            return True
        searched.add(real)
        if not chain(real):
            return False
        for root, dirs, files in os.walk(real):
            for p in (os.path.join(root, name) for name in dirs + files):
                if not node(p):
                    return False
                to = os.path.realpath(p)
                if not os.path.islink(p) or not os.path.exists(to):
                    continue
                if not chain(to) or (os.path.isdir(to) and not tree(to)):
                    return False
        return True

    return not os.path.lexists(path) or tree(path)


@needs_bash
def test_a_root_owned_interpreter_is_safe():
    candidates = [shutil.which(name, path="/usr/bin:/bin") for name in ("python3",)]
    real = next((os.path.realpath(c) for c in candidates if c), None)
    if real is None or os.stat(real).st_uid != 0:
        pytest.skip("no root-owned python3 in /usr/bin")
    if any(os.stat(p).st_uid != 0 or os.stat(p).st_mode & 0o022
           for p in [real, *Path(real).parents]):
        pytest.skip(f"{real} is not root's alone here")
    # P6: its standard library, where its links lead, and ensurepip's wheels; not its site
    # directories, which get a warning at most (site_unsafe).
    prefix = Path(real).parents[1]
    wheels = subprocess.run([real, "-I", "-S", "-c", "import sysconfig; print(sysconfig.get_config_var('WHEEL_PKG_DIR') "
                             "or '')"], capture_output=True, text=True, timeout=60).stdout.strip()
    for path in [*map(str, prefix.glob("lib*/python3*")), wheels]:
        if path and not root_alone(path):
            pytest.skip(f"{path} is not root's alone here")
    r = sourced(f"python_unsafe {shlex.quote(real)}")
    assert r.returncode == 1 and r.stdout == "", (r.stdout, r.stderr)


@needs_bash
@pytest.mark.parametrize("python, ok", [("python3", True), ("bin/python3", False), ("/nonexistent/python3", False)])
def test_resolve_python(python, ok):
    r = sourced(f"OPT_PYTHON={shlex.quote(python)} && resolve_python")
    assert (r.returncode == 0) is ok, r.stderr
    if ok:
        assert r.stdout.strip() == python3_on_path()


@needs_bash
def test_a_venv_another_user_can_change_is_unsafe(tmp_path):
    """F5: nothing in an existing venv runs before only root is known to be able to change it."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    give_away(venv)
    r = sourced(f"unsafe_tree {shlex.quote(str(venv))}")
    assert r.returncode == 0 and r.stdout.strip() == str(venv)


def make_venv(path: Path, executable: str, version: str, python: str | None = None, pip: bool = True) -> Path:
    (path / "bin").mkdir(parents=True)
    (path / "pyvenv.cfg").write_text(f"home = {os.path.dirname(executable)}\ninclude-system-site-packages = false\n"
                                     f"version = {version}\nexecutable = {executable}\n")
    (path / "bin" / "python").symlink_to(python or executable)
    if pip:  # a stand-in that answers python -m pip --version
        site = path / "lib" / ("python%d.%d" % sys.version_info[:2]) / "site-packages" / "pip"
        site.mkdir(parents=True)
        (site / "__init__.py").write_text("")
        (site / "__main__.py").write_text("print('pip 0 (stand-in)')\n")
    return path


REAL_PYTHON = os.path.realpath(sys.executable)
VERSION = "%d.%d.%d" % sys.version_info[:3]


@needs_bash
@pytest.mark.parametrize("cfg_exe, cfg_version, link, reason", [
    ("/usr/local/bin/python3.13-old", VERSION, None, "it was built from /usr/local/bin/python3.13-old, not"),
    (REAL_PYTHON, "3.10.2", None, "it was built for Python 3.10.2, not " + VERSION),
    (REAL_PYTHON, VERSION, "/bin/sh", "its bin/python is not " + REAL_PYTHON),
], ids=["interpreter-gone", "other-version", "other-program"])
def test_a_venv_from_another_interpreter_is_rebuilt(tmp_path, cfg_exe, cfg_version, link, reason):
    """F18: a venv whose interpreter is gone or of another version (a distribution upgrade) is
    rebuilt rather than run."""
    venv = make_venv(tmp_path / "venv", cfg_exe, cfg_version, link)
    r = sourced(f"venv_stale {shlex.quote(str(venv))}", PY_REAL=REAL_PYTHON, PY_VERSION=VERSION)
    assert r.returncode == 0 and reason in r.stdout, (r.stdout, r.stderr)


@needs_bash
def test_a_venv_without_pyvenv_cfg_is_rebuilt(tmp_path):
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    r = sourced(f"venv_stale {shlex.quote(str(tmp_path / 'venv'))}", PY_REAL=REAL_PYTHON, PY_VERSION=VERSION)
    assert r.returncode == 0 and "no pyvenv.cfg" in r.stdout


@needs_bash
def test_a_matching_venv_is_reused(tmp_path):
    venv = make_venv(tmp_path / "venv", REAL_PYTHON, VERSION)
    r = sourced(f"venv_stale {shlex.quote(str(venv))}", PY_REAL=REAL_PYTHON, PY_VERSION=VERSION)
    assert r.returncode == 1 and r.stdout == "", (r.stdout, r.stderr)


@needs_bash
def test_a_venv_of_another_patch_release_is_reused(tmp_path):
    """F18: a patch release of the same X.Y keeps the venv working (bin/python leads to the same
    interpreter, site-packages is the same directory), so it is not rebuilt: a rebuild needs the
    network, and an offline re-run must not lose a working venv over it."""
    major, minor, patch = sys.version_info[:3]
    venv = make_venv(tmp_path / "venv", REAL_PYTHON, f"{major}.{minor}.{patch + 7}")
    r = sourced(f"venv_stale {shlex.quote(str(venv))}", PY_REAL=REAL_PYTHON, PY_VERSION=VERSION)
    assert r.returncode == 1 and r.stdout == "", (r.stdout, r.stderr)


@needs_bash
def test_a_venv_without_pip_is_rebuilt(tmp_path):
    """F18: what a build that failed or was cut short leaves (pyvenv.cfg and bin/python, no pip)
    is not reused; the pip step would fail on it at every re-run."""
    venv = make_venv(tmp_path / "venv", REAL_PYTHON, VERSION, pip=False)
    r = sourced(f"venv_stale {shlex.quote(str(venv))}", PY_REAL=REAL_PYTHON, PY_VERSION=VERSION)
    assert r.returncode == 0 and "it has no working pip" in r.stdout, (r.stdout, r.stderr)


@needs_bash
def test_the_installer_knows_its_own_units(tmp_path):
    """F12: the production unit is told from a development unit by its first line."""
    marker = sourced('printf "%s\\n" "$UNIT_MARKER"').stdout.strip()
    assert UNIT.read_text(encoding="utf-8").splitlines()[0] == marker
    assert sourced(f"production_unit {shlex.quote(str(UNIT))}").returncode == 0
    assert sourced(f"production_unit {shlex.quote(str(DEV_UNIT))}").returncode != 0
    link = tmp_path / "webspec-gateway.service"
    link.symlink_to(UNIT)  # systemctl link: a file someone else may own
    assert sourced(f"production_unit {shlex.quote(str(link))}").returncode != 0


@needs_bash
@pytest.mark.parametrize("content, ok", [
    ("WEBSPEC_GUARD_KEY=\n", False),
    ("WEBSPEC_GUARD_KEY=''\n", False),
    ("#WEBSPEC_GUARD_KEY=abc\n", False),
    ("WEBSPEC_GUARD_KEY=abc\n", True),
    ("WEBSPEC_GUARD_KEY=abc\nWEBSPEC_GUARD_KEY=\n", False),  # the last assignment wins
    # P8: what the gateway counts as no key, though the bytes are not ASCII whitespace.
    ("WEBSPEC_GUARD_KEY=​\n", False),
    ("WEBSPEC_GUARD_KEY= \n", False),
    ("WEBSPEC_GUARD_KEY=﻿\n", False),
    ("WEBSPEC_GUARD_KEY=\"⁠ 　​​\"\n", False),
    ("WEBSPEC_GUARD_KEY=​abc\n", True),  # derives as it always has (GD-5)
    ("WEBSPEC_GUARD_KEY=é\n", True),
    ("WEBSPEC_GUARD_KEY=\U0001f511\n", True),
])
def test_guard_key_set(tmp_path, content, ok):
    env = tmp_path / "gateway.env"
    env.write_bytes(content.encode())
    r = sourced(f"guard_key_set {shlex.quote(str(env))}")
    assert (r.returncode == 0) is ok and r.stdout == ""


# What the gateway counts as no key (webspec.config._invisible: whitespace and format characters)
# on any Python from 3.11 to 3.14, which is what install.sh's BLANK_UTF8 holds: the union of
# their rules. They differ in one place: U+13439-1343F are format characters from Unicode 15.0
# (Python 3.12) on, and unassigned in Python 3.11's Unicode 14.0.
BLANK_RULE = """
0009-000D 001C-0020 0085 00A0 00AD 0600-0605 061C 06DD 070F 0890-0891 08E2 1680 180E 2000-200F
2028-202F 205F-2064 2066-206F 3000 FEFF FFF9-FFFB 110BD 110CD 13430-1343F 1BCA0-1BCA3 1D173-1D17A
E0001 E0020-E007F
"""


def code_points(ranges: str) -> set[int]:
    """The code points of "0009-000D 0085 ..."."""
    out: set[int] = set()
    for item in ranges.split():
        first, _, last = item.partition("-")
        out.update(range(int(first, 16), int(last or first, 16) + 1))
    return out


def unicode_runs(points: set[int]) -> list[tuple[int, int]]:
    runs: list[list[int]] = []
    for c in sorted(points):
        if runs and runs[-1][1] == c - 1:
            runs[-1][1] = c
        else:
            runs.append([c, c])
    return [(a, b) for a, b in runs]


def test_the_blank_rule_is_the_gateways_on_this_python():
    """BLANK_RULE holds every code point the gateway, run by this Python, counts as no key, and
    beyond those only code points this Python's Unicode has not assigned yet (U+13439-1343F on
    3.11). A Python whose gateway counts more fails here: add those to BLANK_UTF8 too."""
    import unicodedata
    from webspec.config import _invisible
    rule = code_points(BLANK_RULE)
    here = {c for c in range(1, 0x110000) if not 0xD800 <= c <= 0xDFFF and _invisible(chr(c))}
    assert not here - rule, ("the gateway counts as no key, and BLANK_RULE does not (add them to BLANK_UTF8 too): "
                             + " ".join(f"U+{c:04X}" for c in sorted(here - rule)))
    assigned = [c for c in sorted(rule - here) if unicodedata.category(chr(c)) != "Cn"]
    assert not assigned, "BLANK_RULE counts as no key what the gateway counts as a key: " + " ".join(
        f"U+{c:04X}" for c in assigned)


@needs_bash
def test_guard_key_set_agrees_with_the_gateway_on_every_character(tmp_path):
    """P8: a value of nothing but such characters made the installer start a gateway that has no
    key. guard_key_set counts as no key every code point of the gateway's rule (BLANK_RULE, held
    to the running Python's rule above), and as a key the code points on either side of each run
    of them. It is the same on every Python: the union of their rules, where Python 3.11, whose
    Unicode leaves U+13439-1343F unassigned, counted U+13439 as a neighbour that must be a key."""
    blank = code_points(BLANK_RULE)
    visible = {c for a, b in unicode_runs(blank) for c in (a - 1, b + 1)} - blank - {0, *range(0xD800, 0xE000)}
    files = tmp_path / "env"
    files.mkdir()
    for c in blank | visible:
        (files / f"{c:06X}").write_bytes(b"WEBSPEC_GUARD_KEY=" + chr(c).encode() + b"\n")
    r = sourced(f'for f in {shlex.quote(str(files))}/*; do if guard_key_set "$f"; then echo "${{f##*/}}"; fi; done')
    assert r.returncode == 0, r.stderr
    set_ = {int(name, 16) for name in r.stdout.split()}
    assert not set_ & blank, "counted as a key: " + " ".join(f"U+{c:04X}" for c in sorted(set_ & blank))
    assert not visible - set_, "counted as no key: " + " ".join(f"U+{c:04X}" for c in sorted(visible - set_))
    assert len(blank) == 199 and len(visible) >= 40


# ── install.sh, the functions of a real run, with stand-ins ──────────────────
#
# These run what a real install runs, sourced, with stand-ins for the commands that would touch
# the host. systemctl answers from a state directory (unit properties, which units are active or
# enabled, which fail to start) and records every call in $STATE/calls; ss shows systemd on
# 127.0.0.1:7002 while the socket is "active"; getent, id, pid_uid, pgrep, journalctl, sleep
# and http_status answer from files.

STUBS = r"""
calls() { printf '%s\n' "$*" >>"$STATE/calls"; }
systemctl() {
  calls "systemctl $*"
  local unit="${@: -1}" u p
  case "$1" in
    show)
      if [[ "$2" == -p ]]; then
        awk -v k="$5 $3=" 'index($0, k) == 1 { print substr($0, length(k) + 1) }' "$STATE/props"
      else
        u="$2"
        shift 2
        while (($#)); do
          if [[ "$1" == -p ]]; then
            p="$2"
            awk -v k="$u $p=" -v p="$p" 'index($0, k) == 1 { print p "=" substr($0, length(k) + 1) }' "$STATE/props"
            shift
          fi
          shift
        done
      fi
      ;;
    is-active) [[ -e "$STATE/active.$unit" ]] ;;
    is-enabled) [[ -e "$STATE/enabled.$unit" ]] ;;
    enable) touch "$STATE/enabled.$unit" ;;
    disable) rm -f "$STATE/enabled.$unit" ;;
    start | restart)
      [[ ! -e "$STATE/fail.$unit" ]] || return 1
      touch "$STATE/active.$unit"
      ;;
    stop)
      shift
      for u in "$@"; do rm -f "$STATE/active.$u"; done
      ;;
  esac
}
ss() {
  if [[ -e "$STATE/active.webspec-gateway.socket" ]]; then
    printf '%s\n' 'LISTEN 0      4096   127.0.0.1:7002 0.0.0.0:* users:(("systemd",pid=1,fd=36))'
  fi
  if [[ -e "$STATE/ss-extra" ]]; then cat "$STATE/ss-extra"; fi
  return 0
}
pgrep() { [[ -e "$STATE/pgrep" ]] && cat "$STATE/pgrep"; }
journalctl() { calls "journalctl $*"; }
sleep() { :; }
http_status() { echo 200; }
pid_uid() { cat "$STATE/uid.$1" 2>/dev/null; }
id() { if [[ "$1" == -u && "$2" == webspec ]]; then echo 999; else return 1; fi; }
getent() {
  case "$1" in
    passwd) if (($# > 1)); then awk -F: -v k="$2" '$1 == k || $3 == k' "$STATE/passwd" | grep .; else cat "$STATE/passwd"; fi ;;
    shadow) grep "^$2:" "$STATE/shadow" ;;
    group) grep "^$2:" "$STATE/group" ;;
  esac
}
"""

PASSWD = "root:x:0:0:root:/root:/bin/bash\nwebspec:x:999:999:WebSpec gateway:/var/lib/webspec:/usr/sbin/nologin\n" \
         "agent:x:1000:1000::/home/agent:/bin/bash\n"


class Host:
    """A state directory for the stand-ins, and a way to run install.sh's functions against it."""

    def __init__(self, path: Path):
        self.state = path
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "passwd").write_text(PASSWD)
        (self.state / "shadow").write_text("webspec:!:20000::::::\n")
        (self.state / "group").write_text("webspec:x:999:\n")
        (self.state / "props").write_text("")

    def set(self, **flags: bool) -> "Host":
        """set(active_webspec_gateway_socket=True, ...): flags are files named like active.<unit>."""
        for name, on in flags.items():
            kind, unit = name.split("_", 1)
            unit = unit.replace("_", "-").replace("-service", ".service").replace("-socket", ".socket")
            f = self.state / f"{kind}.{unit}"
            f.touch() if on else f.unlink(missing_ok=True)
        return self

    def props(self, text: str) -> "Host":
        (self.state / "props").write_text(text)
        return self

    def write(self, name: str, text: str) -> "Host":
        (self.state / name).write_text(text)
        return self

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess:
        return sourced(STUBS + "\n" + script, STATE=str(self.state), DRY_RUN="0", **env)

    def calls(self) -> list[str]:
        f = self.state / "calls"
        return f.read_text().splitlines() if f.exists() else []


@pytest.fixture
def host(tmp_path) -> Host:
    return Host(tmp_path / "host")


def unit_props() -> str:
    """systemctl show's view of the shipped units, installed as install.sh installs them, with no
    drop-ins (the formats are systemd 257's, checked against a real one)."""
    service = parse_unit(UNIT.read_text(encoding="utf-8"))["Service"]
    key_check = shlex.split(service["ExecStartPre"][0])[2]
    exec_tail = " ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    s, k = "webspec-gateway.service", "webspec-gateway.socket"
    return "\n".join([
        f"{s} LoadState=loaded",
        f"{s} FragmentPath=/etc/systemd/system/webspec-gateway.service",
        f"{s} DropInPaths=",
        f"{s} User=webspec",
        f"{s} Group=webspec",
        f"{s} DynamicUser=no",
        f"{s} ExecStartEx={{ path=/opt/webspec/venv/bin/python ; argv[]=/opt/webspec/venv/bin/python -I -m webspec "
        f"; flags={exec_tail}",
        f"{s} ExecStartPreEx={{ path=/bin/sh ; argv[]=/bin/sh -c {key_check} ; flags={exec_tail}",
        f"{s} EnvironmentFiles=/etc/webspec/gateway.env (ignore_errors=no)",
        f"{s} Environment={' '.join(service['Environment'])}",  # the Environment= lines, in order
        f"{s} UnsetEnvironment={' '.join(service['UnsetEnvironment'])}",
        f"{s} NoNewPrivileges=yes",
        f"{s} ProtectSystem=strict",
        f"{s} ProtectHome=yes",
        f"{s} PrivateTmp=yes",
        f"{s} ProtectProc=invisible",
        f"{s} CapabilityBoundingSet=",
        f"{k} LoadState=loaded",
        f"{k} FragmentPath=/etc/systemd/system/webspec-gateway.socket",
        f"{k} DropInPaths=",
        f"{k} Listen=127.0.0.1:7002 (Stream)",
        f"{k} Accept=no",
        f"{k} ReusePort=no",
        f"{k} Triggers=webspec-gateway.service",
    ]) + "\n"


def with_props(base: str, *lines: str, drop: tuple[str, ...] = ()) -> str:
    """base with the lines that start with one of drop removed, and lines added."""
    kept = [ln for ln in base.splitlines() if not ln.startswith(drop)] if drop else base.splitlines()
    return "\n".join(kept + list(lines)) + "\n"


@needs_bash
def test_the_shipped_units_pass_the_effective_check(host):
    r = host.props(unit_props()).run("effective_unit_problems")
    assert r.returncode == 0 and r.stdout == "", (r.stdout, r.stderr)


S = "webspec-gateway.service"
# The service unit's own Environment= and UnsetEnvironment= lines, as systemctl show joins them.
ENV = " ".join(re.findall(r"^Environment=(.*)$", UNIT.read_text(encoding="utf-8"), re.M))
UNSET = " ".join(re.findall(r"^UnsetEnvironment=(.*)$", UNIT.read_text(encoding="utf-8"), re.M))


@needs_bash
@pytest.mark.parametrize("change, problem", [
    # The repro: a leftover drop-in reads a login user's file after gateway.env, guard key included.
    ((f"{S} DropInPaths=/etc/systemd/system/webspec-gateway.service.d/dev.conf",
      f"{S} EnvironmentFiles=/etc/webspec/gateway.env (ignore_errors=no)",
      f"{S} EnvironmentFiles=/home/human/.webspec/gateway.env (ignore_errors=yes)"),
     "reads the environment files /etc/webspec/gateway.env (ignore_errors=no), "
     "/home/human/.webspec/gateway.env (ignore_errors=yes), not /etc/webspec/gateway.env alone"),
    ((f"{S} User=human",), "runs as human:webspec, not webspec:webspec"),
    ((f"{S} DynamicUser=yes",), "has DynamicUser=yes"),
    # ExecStart=@/other ... keeps the argv and changes the program; + runs it as root, unsandboxed.
    ((f"{S} ExecStartEx={{ path=/usr/bin/evil ; argv[]=/opt/webspec/venv/bin/python -I -m webspec ; flags= ; "
      "start_time=[n/a] }",), "runs ExecStart={ path=/usr/bin/evil"),
    ((f"{S} ExecStartEx={{ path=/opt/webspec/venv/bin/python ; argv[]=/opt/webspec/venv/bin/python -I -m webspec ; "
      "flags=privileged ; start_time=[n/a] }",), "flags=privileged"),
    ((f"{S} ExecStartPreEx={{ path=/bin/true ; argv[]=/bin/true ; flags= ; start_time=[n/a] }}",),
     "not only its guard-key check"),
    ((f"{S} ExecStartPostEx={{ path=/bin/sh ; argv[]=/bin/sh -c id ; flags=privileged ; start_time=[n/a] }}",),
     "runs ExecStartPost={ path=/bin/sh"),
    ((f"{S} BindReadOnlyPaths=/home/human/venv:/opt/webspec/venv:rbind",), "has BindReadOnlyPaths="),
    ((f"{S} ProtectHome=no",), "has ProtectHome=no, not yes"),
    ((f"{S} FragmentPath=/run/systemd/transient/webspec-gateway.service",),
     "loads webspec-gateway.service from /run/systemd/transient/webspec-gateway.service"),
    ((f"{S} LoadState=masked",), "webspec-gateway.service is masked, not loaded"),
    (("webspec-gateway.socket Listen=0.0.0.0:7002 (Stream)",), "listens on 0.0.0.0:7002 (Stream), not 127.0.0.1"),
    (("webspec-gateway.socket Accept=yes",), "webspec-gateway.socket has Accept=yes, not no"),
    (("webspec-gateway.socket Triggers=other.service",), "has Triggers=other.service"),
    (("webspec-gateway.socket ExecStartPre={ path=/bin/sh ; argv[]=/bin/sh -c id ; ignore_errors=no }",),
     "webspec-gateway.socket runs ExecStartPre="),
    (("webspec-gateway.socket DropInPaths=/run/systemd/system/socket.d/x.conf",),
     "webspec-gateway.socket has the drop-in /run/systemd/system/socket.d/x.conf"),
    # P8: a drop-in that lets the gateway serve without a usable key, or lets another source in.
    ((f"{S} Environment=HOME=/var/lib/webspec WEBSPEC_REQUIRE_GUARD_KEY=0",),
     "does not set WEBSPEC_REQUIRE_GUARD_KEY=1"),
    ((f"{S} UnsetEnvironment=",), "does not unset WEBSPEC_GUARD_KEY_FILE"),
    ((f"{S} UnsetEnvironment=WEBSPEC_GUARD_KEY_FILE",), "does not unset WEBSPEC_GUARD_KEY_DEV_EPHEMERAL"),
    # Review: an admitted drop-in took the requirement away while the substring checks passed.
    ((f"{S} UnsetEnvironment={UNSET} WEBSPEC_REQUIRE_GUARD_KEY",),
     f"has UnsetEnvironment={UNSET} WEBSPEC_REQUIRE_GUARD_KEY, not only its own: {UNSET}"),
    # As systemd 257 shows such a drop-in: the 0 replaces the 1 in place, and the decoy follows.
    ((f'{S} Environment={ENV.replace("_GUARD_KEY=1", "_GUARD_KEY=0")} "X=a WEBSPEC_REQUIRE_GUARD_KEY=1 b"',),
     "not only its own: " + ENV),
    ((f"{S} Environment={ENV} LD_PRELOAD=/var/lib/webspec/x.so",), "not only its own: " + ENV),
])
def test_the_effective_check_catches(host, change, problem):
    """F12 and the drop-in gap (GD-5, DP-1, DP-4, DP-9): whatever a drop-in, a transient unit or
    a mask changes in who runs what, from which environment files, in which sandbox, or where the
    socket listens, is reported before anything is enabled."""
    keys = tuple(ln.split("=", 1)[0] + "=" for ln in change)
    r = host.props(with_props(unit_props(), *change, drop=keys)).run("effective_unit_problems")
    assert r.returncode == 0 and problem in r.stdout, (r.stdout, r.stderr)


@needs_bash
def test_drop_ins_are_refused_unless_allowed_and_the_rest_still_applies(host):
    dropin = "/etc/systemd/system/webspec-gateway.service.d/50-MemoryMax.conf"
    host.props(with_props(unit_props(), f"{S} DropInPaths={dropin}", drop=(f"{S} DropInPaths=",)))
    r = host.run("effective_unit_problems")
    assert r.stdout == f"{S} has the drop-in {dropin}\n", r.stderr
    r = host.run("OPT_ALLOW_DROP_INS=1 && effective_unit_problems")
    assert r.stdout == "", r.stderr  # a reviewed drop-in that changes nothing above
    host.props(with_props(unit_props(), f"{S} DropInPaths={dropin}", f"{S} User=human",
                          drop=(f"{S} DropInPaths=", f"{S} User=")))
    r = host.run("OPT_ALLOW_DROP_INS=1 && effective_unit_problems")
    assert "runs as human:webspec" in r.stdout and "drop-in" not in r.stdout
    # The distribution's own drop-ins (in /usr/lib/systemd) are not the installer's to refuse.
    host.props(with_props(unit_props(), f"{S} DropInPaths=/usr/lib/systemd/system/service.d/10-timeout-abort.conf",
                          drop=(f"{S} DropInPaths=",)))
    assert host.run("effective_unit_problems").stdout == ""


@needs_bash
def test_check_units_stops_before_anything_is_enabled(host):
    host.props(with_props(unit_props(), f"{S} DropInPaths=/etc/systemd/system/webspec-gateway.service.d/dev.conf",
                          drop=(f"{S} DropInPaths=",)))
    r = host.run("check_units; echo went on")
    assert r.returncode == 1 and "went on" not in r.stdout
    assert "has the drop-in /etc/systemd/system/webspec-gateway.service.d/dev.conf" in r.stderr
    assert "--allow-drop-ins" in r.stderr and "Nothing was enabled or started." in r.stderr
    assert not [c for c in host.calls() if c.startswith(("systemctl enable", "systemctl start", "systemctl restart"))]



@needs_bash
def test_under_allow_drop_ins_the_refusal_does_not_suggest_it(host):
    """Review: a run with --allow-drop-ins was told to keep its drop-ins with --allow-drop-ins."""
    dropin = "/etc/systemd/system/webspec-gateway.service.d/50-env.conf"
    host.props(with_props(unit_props(), f"{S} DropInPaths={dropin}", f"{S} Environment={ENV} LD_PRELOAD=/x.so",
                          drop=(f"{S} DropInPaths=", f"{S} Environment=")))
    r = host.run("OPT_ALLOW_DROP_INS=1 && check_units; echo went on")
    assert r.returncode == 1 and "went on" not in r.stdout
    assert "not only its own" in r.stderr
    assert "--allow-drop-ins admits drop-ins you have reviewed, but not these" in r.stderr
    assert "keep drop-ins you have reviewed with --allow-drop-ins" not in r.stderr
    assert "Variables for the gateway belong in\n/etc/webspec/gateway.env." in r.stderr
    assert "Nothing was\nenabled or started." in r.stderr

# hold_port: when the port is bound anew, and what happens meanwhile.

@needs_bash
def test_an_unchanged_socket_keeps_the_port(host):
    """F2: a re-run, with or without comment changes, never frees 127.0.0.1:7002."""
    host.set(active_webspec_gateway_socket=True, active_cloudflared_service=True, active_webspec_gateway_service=True)
    r = host.run("SOCKET_CHANGED=0 && hold_port")
    assert r.returncode == 0, r.stderr
    assert "webspec-gateway.socket keeps 127.0.0.1:7002 bound." in r.stdout
    assert [c for c in host.calls() if not c.startswith("systemctl is-active")] == [
        "systemctl enable --quiet webspec-gateway.socket"]


@needs_bash
def test_a_changed_socket_is_bound_anew_with_the_tunnel_stopped(host):
    host.set(active_webspec_gateway_socket=True, active_cloudflared_service=True, active_webspec_gateway_service=True)
    r = host.run("SOCKET_CHANGED=1 && hold_port")
    assert r.returncode == 0, r.stderr
    acts = [c for c in host.calls() if not c.startswith("systemctl is-active")]
    assert acts == ["systemctl enable --quiet webspec-gateway.socket",
                    "systemctl stop cloudflared.service",
                    "systemctl stop webspec-gateway.service webspec-gateway.socket",
                    "systemctl start webspec-gateway.socket",
                    "systemctl start cloudflared.service"]
    assert "Stopped cloudflared.service while 127.0.0.1:7002 changes hands (DP-9)." in r.stdout


@needs_bash
def test_a_gateway_that_bound_the_port_itself_hands_it_to_systemd(host):
    """F2: the first run after an earlier install (the development gateway binds 7002 itself): the
    tunnel is stopped, the old gateway stopped, the socket started, the tunnel started again."""
    host.set(active_webspec_gateway_service=True, active_cloudflared_service=True)
    host.write("ss-extra", 'LISTEN 0 2048 127.0.0.1:7002 0.0.0.0:* users:(("python",pid=4242,fd=6))\n')
    r = host.run("SOCKET_CHANGED=1 && hold_port")
    assert r.returncode == 0, r.stderr
    acts = [c for c in host.calls() if c.startswith(("systemctl stop", "systemctl start"))]
    assert acts == ["systemctl stop cloudflared.service",
                    "systemctl stop webspec-gateway.service webspec-gateway.socket",
                    "systemctl start webspec-gateway.socket",
                    "systemctl start cloudflared.service"]


@needs_bash
def test_a_free_port_is_taken_without_touching_the_tunnel(host):
    host.set(active_cloudflared_service=True)
    r = host.run("SOCKET_CHANGED=1 && hold_port")
    assert r.returncode == 0, r.stderr
    assert "cloudflared.service" not in " ".join(c for c in host.calls() if not c.startswith("systemctl is-active"))
    assert "systemctl start webspec-gateway.socket" in host.calls()


@needs_bash
def test_a_port_that_systemd_cannot_bind_leaves_the_tunnel_stopped(host):
    """Someone else holds 127.0.0.1:7002: the installer stops, and the tunnel it stopped stays
    stopped, so that process gets none of the tunnel's traffic."""
    host.set(active_webspec_gateway_service=True, active_cloudflared_service=True, fail_webspec_gateway_socket=True)
    host.write("ss-extra", 'LISTEN 0 5 127.0.0.1:7002 0.0.0.0:* users:(("python3",pid=694,fd=4))\n')
    r = host.run("SOCKET_CHANGED=1 && hold_port; echo went on")
    assert r.returncode == 1 and "went on" not in r.stdout
    assert "systemd could not bind 127.0.0.1:7002" in r.stderr and "stays stopped" in r.stderr
    assert '("python3",pid=694,fd=4)' in r.stderr  # who holds it
    assert "systemctl start cloudflared.service" not in host.calls()


@needs_bash
def test_a_held_port_that_systemd_does_not_hold_is_bound_anew(host):
    """The socket unit is active but something else listens on the port (ss knows): bind anew."""
    host.set(active_webspec_gateway_socket=True)
    r = host.run('ss() { echo "LISTEN 0 5 127.0.0.1:7002 0.0.0.0:* users:((\\"python3\\",pid=7,fd=3))"; }; '
                 "SOCKET_CHANGED=0 && hold_port; echo done")
    assert "systemctl stop webspec-gateway.service webspec-gateway.socket" in host.calls()
    assert r.returncode == 1 and "systemd could not bind" in r.stderr  # and the stand-in ss never shows systemd


@needs_bash
@pytest.mark.parametrize("installed, changed", [
    ("[Socket]\nListenStream=127.0.0.1:7002\n", False),
    ("# a new comment\n[Socket]\n  ListenStream=127.0.0.1:7002  \n\n; another\n", False),
    ("[Socket]\nListenStream=127.0.0.1:7003\n", True),
    ("[Socket]\nListenStream=127.0.0.1:7002\nBacklog=16\n", True),
    (None, True),
])
def test_only_new_directives_count_as_a_changed_socket(tmp_path, installed, changed):
    """F2: comparing the files byte for byte freed the port for a comment."""
    shipped = tmp_path / "shipped.socket"
    shipped.write_text("# shipped\n[Socket]\nListenStream=127.0.0.1:7002\n")
    path = tmp_path / "installed.socket"
    if installed is not None:
        path.write_text(installed)
    r = sourced(f"socket_changed {shlex.quote(str(path))} {shlex.quote(str(shipped))}")
    assert (r.returncode == 0) is changed, r.stderr


# Starting, verifying, and leaving a keyless gateway alone.

@needs_bash
def test_start_gateway_enables_restarts_and_verifies(host):
    host.set(active_webspec_gateway_socket=True).props("webspec-gateway.service MainPID=4321\n").write("uid.4321", "999")
    r = host.run("start_gateway")
    assert r.returncode == 0, r.stderr
    assert "Started webspec-gateway.service: PID 4321 as webspec, on 127.0.0.1:7002 held by webspec-gateway.socket" \
        in r.stdout
    acts = [c for c in host.calls() if c.startswith(("systemctl enable", "systemctl restart", "systemctl start"))]
    assert acts == ["systemctl enable --quiet webspec-gateway.service", "systemctl restart webspec-gateway.service"]


@needs_bash
@pytest.mark.parametrize("uid, held, message", [
    ("1000", True, "webspec-gateway.service (PID 4321) runs as agent, not webspec"),
    (None, True, "webspec-gateway.service (PID 4321) exited right after it started"),  # crash loop, not a user
    ("999", False, "127.0.0.1:7002 is not held by systemd"),
])
def test_verify_gateway_fails_loudly(host, uid, held, message):
    """F12: the unit's own process, as webspec, on the socket systemd holds; otherwise one clear
    reason and the journal."""
    host.set(active_webspec_gateway_socket=held).props("webspec-gateway.service MainPID=4321\n")
    if uid is not None:
        host.write("uid.4321", uid)
    r = host.run("verify_gateway; echo went on")
    assert r.returncode == 1 and "went on" not in r.stdout
    assert message in r.stderr, r.stderr
    assert any(c.startswith("journalctl") for c in host.calls())


# The gateway's process, gone while the installer waits for its answer: systemd has it waiting to
# restart (MainPID=0), and describes the process that ended with the exit status 3 with which it
# refuses to serve without a key.
EXITS = ('http_status() { rm -f "$STATE/uid.4321"; printf "%s\\n" "webspec-gateway.service MainPID=0" '
         '"webspec-gateway.service ExecMainPID=4321" "webspec-gateway.service ExecMainCode=1" '
         '"webspec-gateway.service ExecMainStatus=3" >"$STATE/props"; return 1; }; ')

# The same over time, as systemd 257 showed it (RestartSec=3): the gateway exits a moment into the
# wait, and systemd describes it until it starts the next one, which it describes from then on.
RESTARTS = r"""
set_props() { printf 'webspec-gateway.service %s\n' "$@" >"$STATE/props.new" && mv -f "$STATE/props.new" "$STATE/props"; }
http_status() {
  command sleep 0.3
  rm -f "$STATE/uid.4321"
  set_props MainPID=0 ExecMainPID=4321 ExecMainCode=1 ExecMainStatus=3
  command sleep 3
  echo 999 >"$STATE/uid.4400"
  set_props MainPID=4400 ExecMainPID=4400 ExecMainCode=0 ExecMainStatus=0
  command sleep 1
  return 1
}
"""


@needs_bash
def test_a_gateway_that_exits_instead_of_answering_fails_the_run(host):
    """P8: the gateway exits before it serves when WEBSPEC_GUARD_KEY holds no key it can use
    (WEBSPEC_REQUIRE_GUARD_KEY=1). The installer said "Started" for one that served without a
    key; now it must say that the gateway exited, how, and what may be wrong, with the journal."""
    host.set(active_webspec_gateway_socket=True).props("webspec-gateway.service MainPID=4321\n").write("uid.4321", "999")
    r = host.run(EXITS + "verify_gateway; echo went on")
    assert r.returncode == 1 and "went on" not in r.stdout and "Started" not in r.stdout
    assert ("webspec-gateway.service (PID 4321) exited right after it started with status 3, before it "
            "answered") in r.stderr, r.stderr
    assert "WEBSPEC_GUARD_KEY in /etc/webspec/gateway.env holds no key the gateway can use" in r.stderr
    assert any(c.startswith("journalctl") for c in host.calls())
    # Still running, but silent: that is said instead.
    host.props("webspec-gateway.service MainPID=4321\n").write("uid.4321", "999")
    r = host.run('http_status() { return 1; }; verify_gateway; echo went on')
    assert r.returncode == 1 and "(PID 4321) does not answer HTTP on 127.0.0.1:7002" in r.stderr, r.stderr


@needs_bash
def test_a_gateway_that_exits_is_reported_while_systemd_still_says_how(host):
    """The P8 review: the installer waited out http_status's 15 s before it looked, and by then
    systemd had started the next gateway (RestartSec=3) and described that one, which ran, so the
    message lost "with status 3" in five runs of five. It looks once a second now."""
    host.set(active_webspec_gateway_socket=True).props("webspec-gateway.service MainPID=4321\n").write("uid.4321", "999")
    r = host.run(RESTARTS + "verify_gateway; echo went on")
    assert r.returncode == 1 and "went on" not in r.stdout and "Started" not in r.stdout
    assert ("webspec-gateway.service (PID 4321) exited right after it started with status 3, before it "
            "answered") in r.stderr, r.stderr


@needs_bash
def test_an_answer_that_takes_a_while_still_counts(host):
    """The looks happen while the installer waits for the answer; they do not cut the wait short."""
    host.set(active_webspec_gateway_socket=True).props("webspec-gateway.service MainPID=4321\n").write("uid.4321", "999")
    r = host.run("http_status() { command sleep 2.2; echo 200; }; verify_gateway")
    assert r.returncode == 0 and "Started webspec-gateway.service: PID 4321" in r.stdout, r.stderr
    assert sum(c == "systemctl show -p MainPID --value webspec-gateway.service" for c in host.calls()) >= 2


@needs_bash
@pytest.mark.parametrize("props, pid, ended", [
    ("ExecMainPID=4321 ExecMainCode=1 ExecMainStatus=3", "4321", " with status 3"),
    ("ExecMainPID=4321 ExecMainCode=2 ExecMainStatus=9", "4321", " on signal 9"),
    ("ExecMainPID=4321 ExecMainCode=1 ExecMainStatus=0", "4321", ""),  # status 0: nothing to tell
    ("ExecMainPID=4400 ExecMainCode=0 ExecMainStatus=0", "4321", ""),  # the next one, running
    ("ExecMainPID=4400 ExecMainCode=1 ExecMainStatus=1", "4321", ""),  # the next one's end, not 4321's
    ("ExecMainPID=4400 ExecMainCode=1 ExecMainStatus=3", "", " with status 3"),  # no PID: the last one
])
def test_how_it_ended_says_how_that_process_ended(host, props, pid, ended):
    host.props("".join(f"webspec-gateway.service {p}\n" for p in props.split()))
    r = host.run(f"how_it_ended {pid}; echo '|'")
    assert r.returncode == 0 and r.stdout == ended + "|\n", r.stderr


@needs_bash
def test_the_exit_reported_is_that_of_the_process_that_exited(host):
    """By the time the installer reports PID 4321, systemd may describe a later process: its
    status is not 4321's to report."""
    host.props("webspec-gateway.service ExecMainPID=4400\nwebspec-gateway.service ExecMainCode=1\n"
               "webspec-gateway.service ExecMainStatus=1\n")
    r = host.run("gateway_exited 4321")
    assert r.returncode == 1 and "(PID 4321) exited right after it started, before it answered" in r.stderr, r.stderr


@needs_bash
def test_how_it_ended_allows_systemd_a_moment_to_reap(host):
    host.props("webspec-gateway.service ExecMainPID=4321\nwebspec-gateway.service ExecMainCode=0\n")
    host.write("props.next", "webspec-gateway.service ExecMainPID=4321\nwebspec-gateway.service ExecMainCode=1\n"
                             "webspec-gateway.service ExecMainStatus=3\n")
    r = host.run('sleep() { mv -f "$STATE/props.next" "$STATE/props" 2>/dev/null || :; }; how_it_ended 4321')
    assert r.stdout == " with status 3", r.stderr
    assert sum(c.startswith("systemctl show") for c in host.calls()) == 2
    (host.state / "calls").unlink()
    host.props("webspec-gateway.service ExecMainPID=4321\nwebspec-gateway.service ExecMainCode=0\n")
    r = host.run('how_it_ended 4321; echo "|"')  # never seen to end: nothing, after a moment
    assert r.stdout == "|\n" and sum(c.startswith("systemctl show") for c in host.calls()) == 5


def keyed_decision(env: Path) -> str:
    """What a real run does once systemd holds the port, as shipped, reading env as gateway.env."""
    tail = main_flow().split("\n  hold_port\n", 1)[1]
    return tail[:tail.rindex("fi\n")].replace('"$ENV_FILE"', shlex.quote(str(env)))


@needs_bash
def test_a_key_of_invisible_characters_starts_nothing(host, tmp_path):
    """P8: with gateway.env's key a U+200B, the installer enabled and started the gateway and
    printed the running next steps. That value is no key: nothing is enabled or started, one
    enabled earlier is disabled, and the next steps begin with putting the key in."""
    env = tmp_path / "gateway.env"
    env.write_bytes("WEBSPEC_GUARD_KEY=​\n".encode())
    host.set(enabled_webspec_gateway_service=True, active_webspec_gateway_socket=True)
    r = host.run(keyed_decision(env))
    assert r.returncode == 0, r.stderr
    assert not [c for c in host.calls() if c.startswith(("systemctl enable", "systemctl restart", "systemctl start"))]
    assert "systemctl disable --quiet webspec-gateway.service" in host.calls()
    assert "holds only whitespace or invisible characters" in r.stdout
    assert next_steps(r.stdout).lstrip().startswith("1. Put the guard key in /etc/webspec/gateway.env")


@needs_bash
def test_a_gateway_that_exits_gets_no_running_next_steps(host, tmp_path):
    """P8: whatever key the shell accepts, the gateway's own rule has the last word; a gateway
    that exits instead of serving ends the run there, without the running next steps."""
    env = tmp_path / "gateway.env"
    env.write_text("WEBSPEC_GUARD_KEY=0123abcd\n")
    host.set(active_webspec_gateway_socket=True).props("webspec-gateway.service MainPID=4321\n").write("uid.4321", "999")
    r = host.run(EXITS + keyed_decision(env))
    assert r.returncode == 1 and "Next steps" not in r.stdout and "Started" not in r.stdout
    assert "exited right after it started with status 3" in r.stderr
    host.props("webspec-gateway.service MainPID=4321\n").write("uid.4321", "999")
    r = host.run(keyed_decision(env))  # the control: the same run, with a gateway that answers
    assert r.returncode == 0 and "Started webspec-gateway.service: PID 4321" in r.stdout
    assert "\nNext steps:\n" in r.stdout and "Put the guard key" not in r.stdout


@needs_bash
def test_a_keyless_run_enables_nothing_and_disables_an_earlier_gateway(host):
    """F17, GD-5: no key, no gateway: one an earlier install enabled is disabled (it could not
    start), and nothing is enabled or started."""
    host.set(enabled_webspec_gateway_service=True, active_webspec_gateway_socket=True)
    r = host.run("leave_keyless")
    assert r.returncode == 0, r.stderr
    acts = [c for c in host.calls() if c.startswith(("systemctl enable", "systemctl start", "systemctl restart",
                                                      "systemctl disable"))]
    assert acts == ["systemctl disable --quiet webspec-gateway.service"]
    assert "Not enabling or starting webspec-gateway.service" in r.stdout and "disabled" in r.stderr


@needs_bash
@pytest.mark.parametrize("state, has, lacks", [
    ("running", ["sudo systemctl restart webspec-gateway.service", "setup-caddy.sh", "DP-3 is not set up here"],
     ["WEBSPEC_GUARD_KEY=%s", "enable --now"]),
    ("keyless", ["WEBSPEC_GUARD_KEY=%s", "sudo systemctl enable --now webspec-gateway.service",
                 "webspec-gateway.socket holds 127.0.0.1:7002 already"], []),
    ("blocked", ["WEBSPEC_GUARD_KEY=%s", "Stop the user-level gateway as its user"], ["enable --now"]),
])
def test_next_steps_fit_the_state(state, has, lacks):
    r = sourced(f"next_steps {state}")
    assert r.returncode == 0, r.stderr
    for text in has:
        assert text in r.stdout, (state, text)
    for text in lacks:
        assert text not in r.stdout, (state, text)


# The account (F15) and a gateway the installer did not write (F12).

@needs_bash
@pytest.mark.parametrize("passwd, shadow, problems", [
    ("webspec:x:999:999::/var/lib/webspec:/usr/sbin/nologin", "webspec:!:1::::::", []),
    ("webspec:x:1500:1500::/home/webspec:/bin/bash", "webspec:$y$j9T$abc:1::::::",
     ["its UID 1500 is in the range of login accounts (1000 and up)", "its login shell is /bin/bash",
      "its home is /home/webspec, not /var/lib/webspec", "it has a password"]),
    ("webspec:x:999:999::/var/lib/webspec:", "webspec::1::::::",
     ["its login shell is /bin/sh (none set)", "it has an empty password, so it can log in without one"]),
    ("webspec:x:1000:1000::/var/lib/webspec:/usr/sbin/nologin", "webspec:*:1::::::",
     ["its UID 1000 is in the range of login accounts (1000 and up)", "it shares UID 1000 with agent"]),
    ("webspec:x:0:0::/var/lib/webspec:/usr/sbin/nologin", "webspec:!:1::::::",
     ["it has UID 0 (root)", "it shares UID 0 with root"]),
])
def test_account_problems(host, tmp_path, passwd, shadow, problems):
    host.write("passwd", "root:x:0:0::/root:/bin/bash\nagent:x:1000:1000::/home/agent:/bin/bash\n" + passwd + "\n")
    host.write("shadow", shadow + "\n")
    defs = tmp_path / "login.defs"
    defs.write_text("UID_MIN\t\t\t 1000\n")
    r = host.run(f"LOGIN_DEFS={shlex.quote(str(defs))} && account_problems")
    assert r.stdout.splitlines() == problems, r.stderr


@needs_bash
def test_a_login_account_is_refused_unless_adopted(host):
    host.write("passwd", "webspec:x:1500:1500::/home/webspec:/bin/bash\n").write("shadow", "webspec:$6$x:1::::::\n")
    r = host.run("check_account; echo went on")
    assert r.returncode == 1 and "went on" not in r.stdout
    assert "refusing to adopt the existing user webspec" in r.stderr and "--allow-existing-user" in r.stderr
    r = host.run("OPT_ALLOW_EXISTING_USER=1 && check_account && echo went on")
    assert r.returncode == 0 and "went on" in r.stdout
    assert "Adopted because of --allow-existing-user." in r.stderr


def foreign_props(load: str, frag: str, active: str, enabled: str, pid: int) -> str:
    return (f"{S} LoadState={load}\n{S} FragmentPath={frag}\n{S} ActiveState={active}\n"
            f"{S} UnitFileState={enabled}\n{S} MainPID={pid}\n")


@needs_bash
@pytest.mark.parametrize("props, uid, foreign, live, why", [
    # The development gateway as a system unit, User=human.
    (foreign_props("loaded", "/etc/systemd/system/webspec-gateway.service", "active", "enabled", 148), "1001",
     True, True, "its main process (PID 148) runs as UID 1001, not webspec"),
    # Its unit file deleted (or masked) while it runs: still refused (the nit on F12).
    (foreign_props("not-found", "", "active", "", 148), "1001", True, True, "its unit is not-found"),
    (foreign_props("masked", "/etc/systemd/system/webspec-gateway.service", "active", "masked", 148), "1001",
     True, True, "its unit is masked"),
    (foreign_props("not-found", "", "inactive", "", 0), None, False, False, ""),
    (foreign_props("masked", "/etc/systemd/system/webspec-gateway.service", "inactive", "masked", 0), None,
     False, False, ""),
    # Stopped and disabled: replaced (saved first).
    (foreign_props("loaded", "/etc/systemd/system/webspec-gateway.service", "inactive", "disabled", 0), None,
     True, False, "is not one this installer wrote"),
])
def test_foreign_unit(host, props, uid, foreign, live, why):
    host.props(props)
    if uid is not None:
        host.write("uid.148", uid)
    r = host.run('if foreign_unit; then echo "foreign $FOREIGN_LIVE [$FOREIGN_FILE] $FOREIGN_WHY"; else echo none; fi')
    assert r.returncode == 0, r.stderr
    if not foreign:
        assert r.stdout == "none\n"
    else:
        assert r.stdout.startswith(f"foreign {int(live)} ") and why in r.stdout, r.stdout


@needs_bash
def test_the_installers_own_gateway_is_not_foreign(host):
    host.props(foreign_props("loaded", "/etc/systemd/system/webspec-gateway.service", "active", "enabled", 148))
    host.write("uid.148", "999")
    r = host.run('production_unit() { true; }; if foreign_unit; then echo foreign; else echo none; fi')
    assert r.stdout == "none\n", r.stderr


@needs_bash
def test_the_migration_steps_keep_the_port_free_only_for_a_moment(host):
    """F2 on the F12 path: the refusal says to write the configuration and the key first, while the
    old gateway still serves, to stop the tunnel, and only then to stop the old gateway and run the
    installer at once, which binds the port through systemd on that run, key or no key."""
    host.props(foreign_props("loaded", "/nonexistent/webspec-gateway.service", "active", "enabled", 148))
    host.write("uid.148", "1001")
    r = host.run("check_foreign_unit; echo went on")
    assert r.returncode == 1 and "went on" not in r.stdout
    steps = r.stderr
    order = [steps.index(s) for s in (
        "While it still runs, write the new configuration as root",
        "WEBSPEC_GUARD_KEY=%s",
        "sudo systemctl stop cloudflared.service",
        "sudo systemctl disable --now webspec-gateway.service && sudo ",
        "sudo systemctl start cloudflared.service")]
    assert order == sorted(order), steps
    host.props(foreign_props("not-found", "", "active", "", 148))  # nothing to disable: stop it
    r = host.run("check_foreign_unit")
    assert "sudo systemctl stop webspec-gateway.service && sudo " in r.stderr


# The interpreter (F6), pip's configuration (F14), and the venv (F5, F18).

@needs_bash
def test_an_unsafe_interpreter_is_refused_before_it_runs(tmp_path):
    """F6: check_python, as a real run calls it, refuses an interpreter that a user other than
    root can change without running it (here every file is the test user's)."""
    exe = tmp_path / "py" / "bin" / "python3.12"
    exe.parent.mkdir(parents=True)
    exe.write_text('#!/bin/sh\ntouch "$0.ran"\nexit 0\n')
    exe.chmod(0o755)
    give_away(exe)  # as root, only a world-writable directory above tmp_path (/tmp) was left
    r = sourced(f"DRY=0 && OPT_PYTHON={shlex.quote(str(exe))} && check_python; echo went on")
    assert r.returncode == 1 and "went on" not in r.stdout
    assert "refusing" in r.stderr and "--allow-nonroot-python" in r.stderr
    assert not (exe.parent / "python3.12.ran").exists()
    # A dry run goes on with a warning, and runs none of it either, not even to ask for its site
    # directories (site_unsafe).
    r = sourced(f"DRY=1 && OPT_PYTHON={shlex.quote(str(exe))} && check_python && echo went on")
    assert "went on" in r.stdout and "a real run refuses" in r.stderr, (r.stdout, r.stderr)
    assert not (exe.parent / "python3.12.ran").exists()
    r = sourced(f"DRY=0 && OPT_PYTHON={shlex.quote(str(exe))} && OPT_ALLOW_NONROOT_PYTHON=1 && "
                "check_python && echo went on")
    assert "Going ahead because of --allow-nonroot-python." in r.stderr


# Where links lead (P6). These must not depend on who runs them: as root every file a test makes
# is root's, and as anyone else every file it makes is someone else's already. So they replace
# unsafe_node and unsafe_entries, which the tests above cover, with stand-ins that flag exactly
# the paths given and what lies below them: everything else counts as root's. What remains is
# the question under test: which paths the link checks hand to those two.

def flagging(*paths: Path) -> str:
    """Shell that redefines unsafe_node and unsafe_entries to flag only paths (physical ones) and
    whatever lies below them."""
    cases = "|".join(f"{shlex.quote(str(p))}|{shlex.quote(str(p))}/*" for p in paths) or "/nothing-flagged"
    tests = " -o ".join(f"-path {shlex.quote(str(p))} -o -path {shlex.quote(str(p) + '/*')}" for p in paths) \
        or "-path /nothing-flagged"
    return (f'unsafe_node() {{ case "$1" in {cases}) return 0 ;; esac; return 1; }}; '
            f'unsafe_entries() {{ find -H "$1" \\( {tests} \\) -print -quit | grep .; }}; ')


def dirs(tmp_path: Path, *names: str) -> list[Path]:
    """Physical directories under tmp_path (unsafe_path compares physical paths)."""
    made = []
    for name in names:
        (tmp_path / name).mkdir(parents=True)
        made.append(Path(os.path.realpath(tmp_path / name)))
    return made


@needs_bash
def test_a_link_counts_by_where_it_leads(tmp_path):
    """P6: Debian's /usr/lib/python3.X/sitecustomize.py is a root-owned link to
    /etc/python3.X/sitecustomize.py, which root's python3 -I imports, and so does the gateway.
    Judged by the link's owner alone, a target that others can change went unseen."""
    lib, etc = dirs(tmp_path, "lib", "etc")
    (etc / "sitecustomize.py").write_text("")
    link = lib / "sitecustomize.py"
    link.symlink_to(etc / "sitecustomize.py")
    for flagged, hit in [(etc / "sitecustomize.py", etc / "sitecustomize.py"), (etc, etc), (link, link)]:
        r = sourced(flagging(flagged) + f"unsafe_path {shlex.quote(str(link))}")
        assert r.returncode == 0 and r.stdout == f"{hit}\n", (flagged, r.stdout, r.stderr)
    r = sourced(flagging() + f"unsafe_path {shlex.quote(str(link))}")
    assert r.returncode == 1 and r.stdout == "", r.stderr
    # Relative, through .., as Debian's _sysconfigdata link is written.
    (lib / "rel.py").symlink_to("../etc/sitecustomize.py")
    r = sourced(flagging(etc) + f"unsafe_path {shlex.quote(str(lib / 'rel.py'))}")
    assert r.stdout == f"{etc}\n", r.stderr


@needs_bash
def test_every_link_on_the_way_counts(tmp_path):
    """A root-owned link to a root-owned link in a directory others can write: they can repoint
    the second. So can they swap a directory that a link to a directory leads through."""
    lib, middle, final = dirs(tmp_path, "lib", "middle", "final")
    (final / "mod.py").write_text("")
    (middle / "mod.py").symlink_to(final / "mod.py")
    (lib / "mod.py").symlink_to(middle / "mod.py")
    for flagged in (middle, final):
        r = sourced(flagging(flagged) + f"unsafe_path {shlex.quote(str(lib / 'mod.py'))}")
        assert r.stdout == f"{flagged}\n", (flagged, r.stdout, r.stderr)
    (lib / "pkg").symlink_to(middle)  # lib/pkg/mod.py: a directory link, then a file link
    r = sourced(flagging(final) + f"unsafe_path {shlex.quote(str(lib / 'pkg' / 'mod.py'))}")
    assert r.stdout == f"{final}\n", r.stderr


@needs_bash
def test_links_that_lead_nowhere_or_round(tmp_path):
    """A missing target counts by the directory it would be created in; a loop counts."""
    lib, etc = dirs(tmp_path, "lib", "etc")
    (lib / "gone.py").symlink_to(etc / "gone.py")
    r = sourced(flagging() + f"unsafe_path {shlex.quote(str(lib / 'gone.py'))}")
    assert r.returncode == 1, r.stdout + r.stderr
    r = sourced(flagging(etc) + f"unsafe_path {shlex.quote(str(lib / 'gone.py'))}")
    assert r.stdout == f"{etc}\n", r.stderr
    (lib / "a").symlink_to(lib / "b")
    (lib / "b").symlink_to(lib / "a")
    r = sourced(flagging() + f"unsafe_path {shlex.quote(str(lib / 'a'))}")
    assert r.returncode == 0 and r.stdout.strip() in (str(lib / "a"), str(lib / "b")), r.stdout + r.stderr
    r = sourced(flagging() + "unsafe_path relative/python3")
    assert r.stdout == "relative/python3\n"  # whoever picks the working directory picks the file


@needs_bash
def test_a_tree_is_judged_by_where_its_links_lead(tmp_path):
    """P6: unsafe_tree judged a link inside a tree by its owner alone (find -H)."""
    lib, etc, pkgs = dirs(tmp_path, "lib", "etc", "pkgs/pkg")
    (etc / "sitecustomize.py").write_text("")
    (lib / "sitecustomize.py").symlink_to(etc / "sitecustomize.py")
    (pkgs / "mod.py").write_text("")
    (lib / "pkg").symlink_to(pkgs)  # a package that lives elsewhere
    (lib / "up").symlink_to("..")  # leads back up: searched once, and the search ends
    (lib / "self").symlink_to(".")
    r = sourced(flagging() + f"unsafe_tree {shlex.quote(str(lib))}")
    assert r.returncode == 1 and r.stdout == "", r.stderr
    r = sourced(flagging(etc / "sitecustomize.py") + f"unsafe_tree {shlex.quote(str(lib))}")
    assert r.stdout == f"{etc / 'sitecustomize.py'}\n", r.stderr
    # In a directory a link leads to, every entry counts, as in the tree itself.
    r = sourced(flagging(pkgs / "mod.py") + f"unsafe_tree {shlex.quote(str(lib))}")
    assert r.stdout == f"{pkgs / 'mod.py'}\n", r.stderr


def fake_python(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    """An interpreter, as Debian lays one out, that records how it runs: prefix/bin/python3.12,
    its standard library with sitecustomize.py a link into etc/, and the site directories and
    wheel directory it names (local/lib/site-packages, its own lib/python3.12/site-packages;
    wheels/). Returns exe, etc, local, site, wheels."""
    prefix, etc, local, wheels = dirs(tmp_path, "py/bin", "etc", "local", "wheels")
    prefix = prefix.parent
    site = local / "lib" / "site-packages"
    site.mkdir(parents=True)
    stdlib = prefix / "lib" / "python3.12"
    (stdlib / "site-packages").mkdir(parents=True)
    (etc / "sitecustomize.py").write_text("")
    (stdlib / "sitecustomize.py").symlink_to(etc / "sitecustomize.py")
    exe = prefix / "bin" / "python3.12"
    exe.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$0.ran"\ncase "$*" in\n'
                   f'  *getsitepackages*) printf "%s\\n" {shlex.quote(str(site))} '
                   f'{shlex.quote(str(stdlib / "site-packages"))} ;;\n'
                   f'  *WHEEL_PKG_DIR*) printf "%s\\n" {shlex.quote(str(wheels) + "/")} ;;\n'
                   'esac\n')
    exe.chmod(0o755)
    return exe, etc, local, site, wheels


@needs_bash
def test_the_interpreter_names_its_wheels_only_once_the_rest_is_cleared(tmp_path):
    """P6: ensurepip installs the venv's pip from WHEEL_PKG_DIR (Debian: /usr/share/python-wheels),
    and root then runs that pip. python_unsafe checks the directory the interpreter names when
    run with -I -S, and runs it only once the executable and its standard library are cleared.
    Its site directories are not python_unsafe's: nothing here reads them (site_unsafe)."""
    exe, etc, local, site, wheels = fake_python(tmp_path)
    ran = Path(str(exe) + ".ran")
    for flagged, hit in [(exe, exe), (etc, etc), (wheels, wheels), (site, None), (local, None), (None, None)]:
        ran.unlink(missing_ok=True)
        r = sourced(flagging(*[flagged] if flagged else []) + f"python_unsafe {shlex.quote(str(exe))}")
        if hit is None:
            assert r.returncode == 1 and r.stdout == "", (flagged, r.stdout, r.stderr)
        else:
            assert r.returncode == 0 and r.stdout == f"{hit}\n", (flagged, r.stdout, r.stderr)
        if flagged in (exe, etc):  # never run before the executable and the standard library are cleared
            assert not ran.exists(), flagged
        else:  # then once, isolated and without site processing, so no .pth file runs
            assert ran.read_text().splitlines() == [
                "-I -S -c import sysconfig; print(sysconfig.get_config_var(\"WHEEL_PKG_DIR\") or \"\")"], flagged


@needs_bash
def test_site_directories_others_can_change_are_named_with_the_way_in(tmp_path):
    """site_unsafe prints the site directory and the first path through which others can change
    it: the directory itself, or one above it, as the group staff's /usr/local on a Debian host
    that keeps /etc/staff-group-for-usr-local. Those in the standard library are python_unsafe's."""
    exe, etc, local, site, wheels = fake_python(tmp_path)
    for flagged, out in [(site, f"{site}\n{site}\n"), (local, f"{site}\n{local}\n"), (None, "")]:
        r = sourced(flagging(*[flagged] if flagged else []) + f"site_unsafe {shlex.quote(str(exe))}")
        assert (r.returncode == 0) is bool(out) and r.stdout == out, (flagged, r.stdout, r.stderr)
    own = exe.parents[1] / "lib" / "python3.12" / "site-packages"  # judged with the standard library
    r = sourced(flagging(own) + f"site_unsafe {shlex.quote(str(exe))}")
    assert r.returncode == 1 and r.stdout == "", r.stderr
    assert Path(str(exe) + ".ran").read_text().splitlines()[-1].startswith("-I -S -c import site;")


@needs_bash
def test_site_directories_others_can_change_are_only_a_warning(tmp_path):
    """The P6 follow-up: on a Debian host that keeps /etc/staff-group-for-usr-local, the group
    staff can change /usr/local, and the site directory /usr/local/lib/python3.X/dist-packages in
    it. Refusing the distribution's Python for that protected nothing: root runs it with -I -S
    only, and the venv, the venv's pip and the gateway read their own site directory. So the run
    goes on, with a warning that says who does read it. ensurepip's wheels are still refused."""
    exe, etc, local, site, wheels = fake_python(tmp_path)
    run = f"DRY=0 && OPT_PYTHON={shlex.quote(str(exe))} && check_python && echo went on"
    r = sourced(flagging(local) + run)
    assert r.returncode == 0 and "went on" in r.stdout, (r.stdout, r.stderr)
    assert f"WARNING: users other than root can change {site}, a site directory of {exe} ({local} is " in r.stderr
    assert "This installer and the gateway never read it" in r.stderr and "a venv of its own" in r.stderr
    assert "refusing" not in r.stderr
    assert all(line.startswith("-I -S ") for line in Path(str(exe) + ".ran").read_text().splitlines())
    r = sourced(flagging() + run)  # nothing others can change: no warning
    assert r.returncode == 0 and "went on" in r.stdout and "WARNING" not in r.stderr, r.stderr
    r = sourced(flagging(wheels) + run)
    assert r.returncode == 1 and "went on" not in r.stdout, (r.stdout, r.stderr)
    assert f"refusing {exe} ({exe}): {wheels} is " in r.stderr and "ensurepip's wheels" in r.stderr


@needs_bash
def test_a_pip_configuration_that_is_a_link_counts_by_where_it_leads(tmp_path):
    etc, elsewhere = dirs(tmp_path, "etc", "elsewhere")
    (elsewhere / "pip.conf").write_text("[global]\n")
    (etc / "pip.conf").symlink_to(elsewhere / "pip.conf")
    r = sourced(flagging(elsewhere) + f"GLOBAL_PIP_CONFIGS={shlex.quote(str(etc / 'pip.conf'))} && pip_config_unsafe")
    assert r.returncode == 0 and r.stdout == f"{elsewhere}\n", r.stderr
    r = sourced(flagging() + f"GLOBAL_PIP_CONFIGS={shlex.quote(str(etc / 'pip.conf'))} && pip_config_unsafe")
    assert r.returncode == 1, r.stdout


@needs_bash
def test_a_pip_configuration_others_can_change_is_refused(tmp_path):
    """F14 follow-up: pip --isolated still reads the global configuration, so it must be root's;
    one the agent can write would send root's pip to the agent's index. (Root's own
    /etc/pip.conf, a mirror say, is used as before.)"""
    conf = tmp_path / "pip.conf"
    conf.write_text("[global]\nindex-url = http://127.0.0.1:9/simple\n")
    give_away(conf, tmp_path)
    r = sourced(f"GLOBAL_PIP_CONFIGS={shlex.quote(str(conf))} && pip_config_unsafe")
    assert r.returncode == 0 and r.stdout.strip() == str(conf)
    missing = tmp_path / "xdg" / "pip" / "pip.conf"  # not there yet: whoever can create it counts
    r = sourced(f"GLOBAL_PIP_CONFIGS={shlex.quote(str(missing))} && pip_config_unsafe")
    assert r.returncode == 0 and r.stdout.strip() == str(tmp_path)
    r = sourced("GLOBAL_PIP_CONFIGS=/nonexistent-webspec-test/pip.conf && pip_config_unsafe")
    assert r.returncode == 1 and r.stdout == ""  # / is root's
    r = sourced(f"DRY=0 && GLOBAL_PIP_CONFIGS={shlex.quote(str(conf))} && check_pip_config; echo went on")
    assert r.returncode == 1 and "could point root's pip at another index" in r.stderr


VENV_TOOLS = r"""
unsafe_entries() { return 1; }   # root owns it all
PY_VERSION=3.12.1
PY_REAL="$STATE/builder"
"""


@pytest.fixture
def venvs(tmp_path) -> tuple[Path, Path]:
    """A host state directory with a builder that makes a 'venv' (a marker file) like python -m venv."""
    state = tmp_path / "state"
    state.mkdir()
    builder = state / "builder"
    builder.write_text('#!/bin/sh\nfor d; do :; done\nmkdir -p "$d/bin" && echo new >"$d/marker"\n')  # the last argument
    builder.chmod(0o755)
    venv = tmp_path / "opt" / "venv"
    venv.mkdir(parents=True)
    (venv / "marker").write_text("old\n")
    return state, venv


@needs_bash
def test_a_failed_rebuild_puts_the_previous_venv_back(venvs):
    """F18: a rebuild keeps the old venv aside until the new one works; if the run fails before
    (pip offline, say), the old one is back in place, and nothing half-built stays."""
    state, venv = venvs
    script = VENV_TOOLS + f'venv_stale() {{ echo "it was built for Python 3.11.9"; }}; prepare_venv {shlex.quote(str(venv))}'
    r = sourced(script + ' && echo "building=$VENV_BUILDING" && cat "$VENV_TARGET/marker" && venv_rollback && '
                'cat "$VENV_TARGET/marker" && ls "$(dirname "$VENV_TARGET")"', STATE=str(state))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        f"Rebuilding {venv}: it was built for Python 3.11.9. The old one waits as {venv}.previous."
        + r.stdout.split(".previous.")[1].split(" ")[0] + " until the new one works.",
        "building=1", "new", "old", "venv"]
    assert "the previous one is back" in r.stderr


@needs_bash
def test_a_finished_rebuild_drops_the_previous_venv(venvs):
    state, venv = venvs
    script = VENV_TOOLS + f'venv_stale() {{ echo stale; }}; prepare_venv {shlex.quote(str(venv))}'
    r = sourced(script + ' && venv_done && venv_rollback && cat "$VENV_TARGET/marker" && ls "$(dirname "$VENV_TARGET")"',
                STATE=str(state))
    assert r.returncode == 0 and r.stdout.splitlines()[-2:] == ["new", "venv"], (r.stdout, r.stderr)


@needs_bash
def test_an_untrusted_venv_is_never_put_back(venvs):
    """F5: a venv that a user other than root could change is never run and never restored."""
    state, venv = venvs
    script = VENV_TOOLS + 'unsafe_entries() { printf "%s\\n" "$1"; }; ' + f"prepare_venv {shlex.quote(str(venv))}"
    r = sourced(script + ' && venv_rollback; ls -A "$(dirname "$VENV_TARGET")"', STATE=str(state))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1:] != ["venv"] and "can be changed by a user other than root" in r.stderr
    assert not venv.exists() and not list(venv.parent.iterdir())


@needs_bash
def test_a_venv_is_judged_by_its_own_entries(tmp_path):
    """P6 and --allow-nonroot-python: a venv's links lead to the interpreter, which check_python
    judges (and the option can accept). Following them here as well would move a sound venv
    aside, and build it again, on every run with that option."""
    venv, interp = dirs(tmp_path, "opt/venv", "home/py/bin")
    (interp / "python3.12").write_text("")
    (venv / "bin").mkdir()
    (venv / "bin" / "python").symlink_to(interp / "python3.12")
    r = sourced(flagging(interp) + f"venv_stale() {{ return 1; }}; PY_VERSION=3.12.1; "
                f"PY_REAL={shlex.quote(str(interp / 'python3.12'))}; prepare_venv {shlex.quote(str(venv))} && "
                'echo "building=$VENV_BUILDING"')
    assert r.returncode == 0 and "Reusing" in r.stdout and "building=0" in r.stdout, (r.stdout, r.stderr)
    r = sourced(flagging(interp) + f"unsafe_tree {shlex.quote(str(venv))}")  # what following them finds
    assert r.stdout == f"{interp}\n", r.stderr


@needs_bash
def test_a_reusable_venv_is_kept(venvs):
    state, venv = venvs
    script = VENV_TOOLS + f'venv_stale() {{ return 1; }}; prepare_venv {shlex.quote(str(venv))}'
    r = sourced(script + ' && echo "building=$VENV_BUILDING" && venv_rollback && cat "$VENV_TARGET/marker"',
                STATE=str(state))
    assert r.stdout.splitlines()[1:] == ["building=0", "old"], (r.stdout, r.stderr)


# The order of a real run (what a dry run cannot show).

def main_flow() -> str:
    text = INSTALL.read_text(encoding="utf-8")
    return text[text.index('parse_args "$@"\n'):]


def test_a_real_run_checks_everything_before_it_changes_anything():
    """F5, F6, F12, F14, F15: the refusals of what is already there (the Python, pip's
    configuration, the account, a unit this repository did not ship) come before the first change."""
    flow = main_flow()
    first_change = flow.index("\nrun ")
    for check in ("check_python\n", "check_pip_config\n", "check_account\n", "check_foreign_unit\n"):
        assert flow.index(check) < first_change, check
    # F6: nothing of the interpreter runs before python_unsafe has cleared it.
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("check_python() {"):text.index("\n}\n", text.index("check_python() {"))]
    assert body.index('python_unsafe "$PY_REAL"') < body.index('"$PY_REAL" -I')


def test_a_real_run_builds_the_venv_under_its_rollback():
    """F5, F18: prepare_venv decides about the old venv before anything in it runs, under the
    trap that puts the previous venv back until venv_done, after the import check."""
    flow = main_flow()
    trap = flow.index("trap venv_rollback EXIT")
    prepare = flow.index('prepare_venv "$VENV"')
    pip = flow.index('run "$VENV/bin/python" -I -m pip')
    check = flow.index("'import webspec.app'")
    done = flow.index("\n  venv_done\n")
    assert trap < prepare < pip < check < done < flow.index("trap - EXIT HUP INT TERM")


def test_a_real_run_holds_the_port_before_it_decides_about_the_key():
    """F2, F17: the units are checked after daemon-reload; the port is held on every run; the
    gateway starts only with its key, and a keyless run never starts it."""
    flow = main_flow()
    at = [flow.index(s) for s in ("run systemctl daemon-reload", "\ncheck_units\n", "  hold_port\n")]
    assert at == sorted(at)
    # The decision, as it stands: nothing else runs on either side of it.
    assert flow.endswith("""
  hold_port
  if guard_key_set "$ENV_FILE"; then
    start_gateway
    next_steps running
  else
    leave_keyless
    next_steps keyless
  fi
fi
""")


# ── The system units ─────────────────────────────────────────────────────────


def parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """{section: {key: [values, in order]}}, as systemd reads a unit file."""
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    logical, pending = [], ""
    for raw in text.splitlines():
        if raw.rstrip().endswith("\\") and not raw.lstrip().startswith(("#", ";")):
            pending += raw.rstrip()[:-1] + " "
            continue
        logical.append(pending + raw)
        pending = ""
    for raw in logical:
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, sep, value = line.partition("=")
        assert sep and current is not None, f"not a directive: {raw!r}"
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


@pytest.fixture(scope="module")
def service() -> dict[str, list[str]]:
    return parse_unit(UNIT.read_text(encoding="utf-8"))["Service"]


def one(service: dict[str, list[str]], key: str) -> str:
    values = service.get(key, [])
    assert len(values) == 1, f"{key}= should be set exactly once, got {values}"
    return values[0]


def test_unit_runs_the_venv_gateway_as_the_service_user(service):
    assert one(service, "User") == "webspec" and one(service, "Group") == "webspec"
    argv = shlex.split(one(service, "ExecStart"))
    assert argv[0] == "/opt/webspec/venv/bin/python" and argv[-2:] == ["-m", "webspec"]
    assert "-I" in argv  # the writable working directory must not be on sys.path
    assert one(service, "Type") == "exec"  # the gateway is the main process: LISTEN_PID is its PID
    assert "DynamicUser" not in service


def test_unit_environment_is_the_contract_and_holds_no_secret(service):
    env = dict(item.split("=", 1) for value in service["Environment"] for item in shlex.split(value))
    assert env == {
        "HOME": "/var/lib/webspec",
        "WEBSPEC_CONFIG": "/etc/webspec/config.json",
        "WEBSPEC_AUDIT_LOG": "/var/lib/webspec/gateway-audit.jsonl",
        "WEBSPEC_APPROVERS_FILE": "/etc/webspec/allowed_signers",
        "WEBSPEC_HOST": "127.0.0.1",  # DP-8: loopback only
        "WEBSPEC_PORT": "7001",
        "WEBSPEC_INTERNAL_PORT": "7002",
        "WEBSPEC_REQUIRE_GUARD_KEY": "1",  # GD-5, P8: no usable key, no gateway
    }  # no WEBSPEC_GUARD_KEY (unit files are world-readable), no WEBSPEC_ACCESS_LOG (DP-6)
    assert one(service, "EnvironmentFile") == "/etc/webspec/gateway.env"  # required: no leading '-'


def test_unit_lets_no_other_source_of_the_key_stand_in(service):
    """GD-5, P8: with WEBSPEC_GUARD_KEY blank, the gateway would fall back to WEBSPEC_GUARD_KEY_FILE
    (a file every stdio server could read) or WEBSPEC_GUARD_KEY_DEV_EPHEMERAL (a throwaway key).
    systemd drops both from the gateway's environment, after gateway.env is read (checked in a
    systemd 257 container: values that the environment file sets are gone)."""
    assert shlex.split(one(service, "UnsetEnvironment")) == ["WEBSPEC_GUARD_KEY_FILE",
                                                            "WEBSPEC_GUARD_KEY_DEV_EPHEMERAL"]
    header = UNIT.read_text(encoding="utf-8").split("[Unit]")[0]
    assert "WEBSPEC_REQUIRE_GUARD_KEY=1" in header and "invisible characters only" in header


def test_unit_state_and_logging(service):
    assert one(service, "StateDirectory") == "webspec"
    assert one(service, "StateDirectoryMode") == "0700"
    assert one(service, "WorkingDirectory") == "/var/lib/webspec"
    assert one(service, "UMask") == "0077"
    # DP-6: the journal only, no log files.
    assert one(service, "StandardOutput") == "journal" and one(service, "StandardError") == "journal"
    assert "LogsDirectory" not in service
    assert "--workers" not in one(service, "ExecStart")  # DP-7: one process


def test_unit_restarts_whatever_ends_the_gateway(service):
    """F3: a gateway ended by SIGTERM or with status 0 (a stdio server of the same user can send
    it the signal) comes back; on-failure would leave it, and the port, down."""
    assert one(service, "Restart") == "always"
    assert one(service, "RestartSec") == "3"
    # A stop signals the gateway alone, so the stdio servers keep answering the calls it drains;
    # the rest of the unit is killed once it has exited, and the stop timeout outlasts the drain.
    assert one(service, "KillMode") == "mixed"
    from webspec.__main__ import GRACEFUL_SHUTDOWN_SECONDS as drain
    assert one(service, "TimeoutStopSec") == "40s" and 40 > drain
    unit = parse_unit(UNIT.read_text(encoding="utf-8"))["Unit"]
    assert unit["StartLimitIntervalSec"] == ["0"]  # a start limit would fail the socket too
    header = UNIT.read_text(encoding="utf-8").split("[Unit]")[0]
    assert "can" in header and "end it" in header and "clears what DP-7 keeps in memory" in header
    # A shutdown that waits on a request ends (webspec/__main__.py), and what no restart covers is said.
    from webspec.__main__ import GRACEFUL_SHUTDOWN_SECONDS
    assert f"waits at most {GRACEFUL_SHUTDOWN_SECONDS} s" in header
    assert "SIGSTOP" in header and "WatchdogSec=" in header


def test_unit_takes_its_socket_from_systemd():
    """F2 (DP-9): the service needs the socket unit and starts after it. No Also=: disabling the
    gateway (a keyless run does) must not disable the socket, which holds the port regardless."""
    unit = parse_unit(UNIT.read_text(encoding="utf-8"))
    assert unit["Unit"]["Requires"] == ["webspec-gateway.socket"]
    assert unit["Unit"]["After"] == ["webspec-gateway.socket network.target"]
    assert unit["Install"] == {"WantedBy": ["multi-user.target"]}


@needs_bash
def test_unit_never_starts_without_the_guard_key(service):
    """GD-5, F17: whatever starts the unit (a connection to the socket, a boot, a restart), it
    runs the gateway only when gateway.env holds a guard key; the installer holds the unit to
    exactly this check (KEY_CHECK)."""
    argv = shlex.split(one(service, "ExecStartPre"))
    assert argv[:2] == ["/bin/sh", "-c"] and len(argv) == 3
    assert argv[2] == sourced('printf "%s" "$KEY_CHECK"').stdout
    assert "$${WEBSPEC_GUARD_KEY-}" in argv[2]  # expanded by the shell from its environment, never into argv


@pytest.mark.parametrize("value, starts", [
    ("", False), ("   ", False), ("\t", False), ("0123abcd", True), (" key with spaces ", True)])
def test_the_guard_key_check_runs_as_the_unit_runs_it(service, value, starts):
    """The check as systemd runs it: "$$" is systemd's escape for "$" (systemd.service(5))."""
    script = shlex.split(one(service, "ExecStartPre"))[2].replace("$$", "$")
    r = subprocess.run(["/bin/sh", "-c", script], env={"WEBSPEC_GUARD_KEY": value, "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True)
    assert (r.returncode == 0) is starts
    assert starts or "WEBSPEC_GUARD_KEY is empty or missing in /etc/webspec/gateway.env" in r.stderr
    r = subprocess.run(["/bin/sh", "-c", script], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert r.returncode == 1  # unset


@pytest.mark.parametrize("path", [UNIT, SOCKET])
def test_unit_headers_say_how_to_stop_the_gateway(path):
    """F2 (DP-9): stopping the socket frees 127.0.0.1:7002 for any local process, so what
    forwards there stops first, and starts last; stopping only the service is no stop."""
    header = path.read_text(encoding="utf-8").split("[Unit]")[0]
    stops = ["sudo systemctl stop cloudflared.service",
             "sudo systemctl stop caddy-webspec.socket caddy-webspec.service",
             "sudo systemctl stop webspec-gateway.socket webspec-gateway.service"]
    at = [header.index(s) for s in stops]
    assert at == sorted(at)
    assert "reverse order" in header
    assert "is no stop" in header and "starts it again" in header


def test_socket_unit_holds_the_gateway_port_on_loopback():
    """F2 (DP-8, DP-9): systemd binds 127.0.0.1:7002 and keeps it while the gateway restarts."""
    unit = parse_unit(SOCKET.read_text(encoding="utf-8"))
    assert unit["Socket"] == {
        "ListenStream": ["127.0.0.1:7002"],  # one socket: the gateway takes exactly fd 3
        "ReusePort": ["no"],
        "TriggerLimitIntervalSec": ["0"],
        "IPAddressDeny": ["any"],
        "IPAddressAllow": ["localhost"],
    }
    assert unit["Install"] == {"WantedBy": ["sockets.target"]}
    assert "Service" not in unit["Socket"]  # the default: webspec-gateway.service
    assert re.search(r"readonly GATEWAY_PORT=7002\b", INSTALL.read_text(encoding="utf-8"))


@pytest.mark.parametrize("key, value", [
    ("NoNewPrivileges", "yes"),
    ("CapabilityBoundingSet", ""),
    ("ProtectSystem", "strict"),
    ("ProtectHome", "yes"),
    ("PrivateTmp", "yes"),
    ("PrivateDevices", "yes"),
    ("ProtectKernelTunables", "yes"),
    ("ProtectKernelModules", "yes"),
    ("ProtectKernelLogs", "yes"),
    ("ProtectControlGroups", "yes"),
    ("ProtectClock", "yes"),
    ("ProtectHostname", "yes"),
    ("ProtectProc", "invisible"),
    ("RestrictSUIDSGID", "yes"),
    ("RestrictNamespaces", "yes"),
    ("RestrictRealtime", "yes"),
    ("LockPersonality", "yes"),
    ("SystemCallArchitectures", "native"),
    ("SystemCallFilter", "@system-service"),
    ("SystemCallErrorNumber", "EPERM"),
    ("RestrictAddressFamilies", "AF_UNIX AF_INET AF_INET6"),
    ("LimitCORE", "0"),
    ("RemoveIPC", "yes"),
])
def test_unit_hardening(service, key, value):
    assert one(service, key) == value


@pytest.mark.parametrize("key", [
    "MemoryDenyWriteExecute",  # Node.js's JIT needs writable+executable memory
    "ProcSubset",  # hides /proc files that Node.js, Go and Python read
    "AmbientCapabilities",
    "ReadWritePaths",  # the state directory is the only writable path
    "PrivateNetwork",  # stdio and http MCP servers need the network
])
def test_unit_avoids(service, key):
    assert key not in service


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_units_verify(tmp_path):
    # Only the venv is missing on a build machine; anything else systemd reports is a defect.
    for path in (UNIT, SOCKET):
        shutil.copy2(path, tmp_path / path.name)
    r = subprocess.run(["systemd-analyze", "verify", str(tmp_path / UNIT.name), str(tmp_path / SOCKET.name)],
                       capture_output=True, text=True)
    complaints = [ln for ln in (r.stdout + r.stderr).splitlines()
                  if ("webspec-gateway" in ln and "/opt/webspec/venv/bin/python is not executable" not in ln)]
    assert complaints == []


# ── The single-user development unit (gateway/systemd/) ─────────────────────


def test_dev_unit_is_marked_and_its_behavior_unchanged():
    text = DEV_UNIT.read_text(encoding="utf-8")
    header = text.split("[Unit]")[0]
    assert "DP-1" in header and "DP-4" in header and "gateway/deploy/" in header
    # A live host may symlink this file: its directives are exactly what they were.
    assert parse_unit(text) == {
        "Unit": {"Description": ["WebSpec Local Gateway — REST interface to MCP servers"],
                 "After": ["network.target"]},
        "Service": {"Type": ["simple"],
                    "ExecStart": ["/usr/bin/python3 -m webspec"],
                    "WorkingDirectory": ["%h/MCP/webspec-gateway"],
                    "Environment": ["PYTHONPATH=%h/MCP/webspec-gateway", "WEBSPEC_PORT=7001",
                                    "WEBSPEC_INTERNAL_PORT=7002"],
                    "EnvironmentFile": ["-%h/.webspec/gateway.env"],
                    "Restart": ["always"],
                    "RestartSec": ["3"],
                    "StandardOutput": ["append:%h/.webspec/gateway.log"],
                    "StandardError": ["append:%h/.webspec/gateway.log"]},
        "Install": {"WantedBy": ["default.target"]},
    }
