"""The macOS production deployment (gateway/deploy/macos) and the dev LaunchAgent it replaces.

The LaunchDaemon must run the gateway as _webspec, in isolated mode, with no secret in its
world-readable plist (DP-1, DP-4, GD-5), on a loopback socket that launchd holds for it (DP-8,
DP-9), with private logs. A real run of install.sh needs root, so these tests drive it in two
ways that need none and also run on Linux CI. With DRY_RUN=1 it only prints what would change the
system: fake dscl and launchctl make its decisions deterministic, and fakes for every mutating
tool (and for tools it must never run) prove that a dry run changes nothing. Sourced, it defines
its functions and runs nothing: the checks a real run makes around loading the daemon, and the
guard-key fill (--fill-key), are called directly, with fakes for sudo, chown, lsof, ps, curl,
install and launchctl, or with one of its own functions redefined to isolate another. What launchd itself does with the plist (binding the
socket, handing it over) needs root and a live system domain, so it is not tested here.
"""
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from webspec import approval
from webspec import config as gw_config
from webspec.config import parse_claude_config

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "gateway"
MACOS = GATEWAY / "deploy" / "macos"
DAEMON_PLIST = MACOS / "com.webspec.gateway.daemon.plist"
INSTALL = MACOS / "install.sh"
EXAMPLE = GATEWAY / "deploy" / "config.example.json"
DEV_PLIST = GATEWAY / "launchd" / "com.webspec.gateway.plist"
INSTALLED_PLIST = "/Library/LaunchDaemons/com.webspec.gateway.daemon.plist"
# macOS /bin/bash is 3.2, the version the installer has to run on.
BASH = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")
CMD = "set -ae; . /etc/webspec/gateway.env; exec /opt/webspec/venv/bin/python -I -m webspec"
SVC_PATH = "/opt/webspec/venv/bin:/usr/bin:/bin:/usr/sbin:/sbin"

non_root = pytest.mark.skipif(os.geteuid() == 0, reason="as root, files the test creates count as root-owned, "
                              "and install.sh restarts in a clean environment that drops the fake tools")


def _guard_key_readable_here() -> bool:
    """Whether a dry run without root gets past the guard key, as load_daemon decides it: not on a
    Mac where WebSpec is installed (/etc/webspec is root:_webspec 0750, guard.key 0440)."""
    etc = "/etc/webspec"
    if not os.path.isdir(etc):
        return True
    key = os.path.join(etc, "guard.key")
    return os.access(etc, os.X_OK) and (not os.path.exists(key) or os.access(key, os.R_OK))


# The plan of a fresh install goes past the guard key, which a dry run without root cannot read
# where WebSpec is installed: there it stops, and says so (test_a_dry_run_without_root_stops_at_
# an_installed_guard_key).
fresh_host = pytest.mark.skipif(not _guard_key_readable_here(),
                                reason="WebSpec is installed here: a dry run without root stops at its guard key")


def _daemon() -> dict:
    return plistlib.loads(DAEMON_PLIST.read_bytes())


def _owned_by_others(path: Path) -> bool:
    """True if path, or a directory above it, belongs to someone other than root or is writable by
    its group or everyone: what install.sh's unsafe_chain looks for."""
    for p in (path, *path.parents):
        st = p.lstat()
        if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return True
    return False


# ── The LaunchDaemon ──


def test_daemon_runs_the_gateway_as_the_service_user():
    d = _daemon()
    assert d["Label"] == "com.webspec.gateway.daemon"
    assert d["UserName"] == "_webspec"
    assert d["GroupName"] == "_webspec"
    assert d["ProgramArguments"] == ["/bin/sh", "-c", CMD, "webspec-gateway"]
    assert "Program" not in d
    assert d["WorkingDirectory"] == "/var/lib/webspec"
    assert d["RunAtLoad"] is True and d["KeepAlive"] is True
    assert d["ProcessType"] == "Standard"
    assert d["Umask"] == 63 == 0o077


def test_daemon_starts_python_in_isolated_mode():
    # The working directory (/var/lib/webspec) is writable by the stdio servers, so it must not
    # be on sys.path: -I, as deploy/linux's unit does.
    sourced, start = CMD.split("; exec ")
    assert sourced == "set -ae; . /etc/webspec/gateway.env"
    assert shlex.split(start) == ["/opt/webspec/venv/bin/python", "-I", "-m", "webspec"]


def test_daemon_environment_is_exactly_the_contract():
    # Exact, so nothing secret (WEBSPEC_GUARD_KEY, a token) and nothing that widens exposure
    # (WEBSPEC_ACCESS_LOG, WEBSPEC_CORS_ORIGINS, WEBSPEC_HOST=0.0.0.0) can slip in. Site settings
    # (WEBSPEC_DOMAIN) and secrets live in /etc/webspec/gateway.env, which the plist sources.
    assert _daemon()["EnvironmentVariables"] == {
        "HOME": "/var/lib/webspec",
        "PATH": SVC_PATH,
        "WEBSPEC_CONFIG": "/etc/webspec/config.json",
        "WEBSPEC_GUARD_KEY_FILE": "/etc/webspec/guard.key",
        "WEBSPEC_AUDIT_LOG": "/var/lib/webspec/gateway-audit.jsonl",
        "WEBSPEC_APPROVERS_FILE": "/etc/webspec/allowed_signers",
        # Pinned: whatever directories gateway.env appends to PATH, they never choose the
        # program that verifies level-4 approvals.
        "WEBSPEC_SSH_KEYGEN": "/usr/bin/ssh-keygen",
        # The gateway takes its socket from launchd (below) and binds nothing itself, so
        # WEBSPEC_HOST is gone; the ports only name the Host headers it answers (app.py).
        "WEBSPEC_LAUNCHD_SOCKET": "gateway",
        "WEBSPEC_PORT": "7001",
        "WEBSPEC_INTERNAL_PORT": "7002",
    }


def test_daemon_lets_launchd_hold_the_gateway_port_on_loopback():
    # DP-9 (finding: port takeover): launchd binds 127.0.0.1:7002 when the job is loaded and keeps
    # it across gateway restarts, so no local process can take the port that Caddy forwards to.
    # One socket, IPv4 loopback only (DP-8), on the port the gateway's Host routes name, under
    # the name WEBSPEC_LAUNCHD_SOCKET gives the gateway (contract C1).
    d = _daemon()
    env = d["EnvironmentVariables"]
    assert d["Sockets"] == {env["WEBSPEC_LAUNCHD_SOCKET"]: {
        "SockNodeName": "127.0.0.1",
        # launchd.plist(5): "a port number represented as an integer".
        "SockServiceName": int(env["WEBSPEC_INTERNAL_PORT"]),
        "SockFamily": "IPv4",
        "SockType": "stream",
    }}
    assert d["RunAtLoad"] is True and d["KeepAlive"] is True


def test_launchd_lets_the_gateway_finish_requests_in_flight_before_it_kills_it():
    # launchd stops the job (bootout, kickstart -k) with SIGTERM and sends SIGKILL ExitTimeOut
    # seconds later; without the key the system picks 5 s (macOS 27). The gateway gives requests in
    # flight GRACEFUL_SHUTDOWN_SECONDS, then exits by itself. A call killed sooner may have run its
    # tool, and the restarted gateway no longer knows its Idempotency-Key (ID-3, ID-5).
    from webspec.__main__ import GRACEFUL_SHUTDOWN_SECONDS
    exit_timeout = _daemon()["ExitTimeOut"]
    assert type(exit_timeout) is int  # launchd.plist(5): an <integer>
    assert exit_timeout == 40 > GRACEFUL_SHUTDOWN_SECONDS
    # The comments give the gateway's own limit.
    plist, install = " ".join(DAEMON_PLIST.read_text().split()), INSTALL.read_text()
    assert f"gives requests in flight up to {GRACEFUL_SHUTDOWN_SECONDS} s" in plist
    assert f"longer than the {GRACEFUL_SHUTDOWN_SECONDS} s the gateway takes at most" in install


def test_daemon_logs_go_to_the_private_log_dir():
    d = _daemon()
    assert d["StandardOutPath"] == d["StandardErrorPath"] == "/var/log/webspec/gateway.log"


def test_plist_and_installer_document_the_key_files_owner():
    # F51: the gateway reads guard.key through its group; the service user owns nothing it could
    # rewrite. The plist's comment and the installer's layout say so, not the old _webspec 0400.
    plist, install = " ".join(DAEMON_PLIST.read_text().split()), INSTALL.read_text()
    assert "/etc/webspec/guard.key (root:_webspec 0440: the gateway reads it through its group" in plist
    assert "#     guard.key            root:_webspec 0440, created empty for you to fill" in install
    assert "0400" not in plist and "_webspec:_webspec 0400" not in install


def test_installer_holds_the_plist_to_the_same_program_and_path():
    # install.sh's check_plist compares the plist with these; it needs plutil, so compare here too.
    text = INSTALL.read_text()
    assert f"GATEWAY_CMD='{CMD}'" in text
    assert f"SVC_PATH={SVC_PATH}\n" in text


FAKE_PYTHON = """#!/bin/sh
printf '%s\\n' "pid=$$" "argv=$*" "TOKEN=$TOKEN" "WEBSPEC_DOMAIN=$WEBSPEC_DOMAIN" \\
    "WEBSPEC_LAUNCHD_SOCKET=$WEBSPEC_LAUNCHD_SOCKET" "PATH=$PATH"
"""


def _run_daemon_command(tmp_path: Path, env_file: Path) -> tuple[subprocess.Popen, str, str]:
    """Run the plist's program as launchd would, with gateway.env and the interpreter swapped for
    test files; returns (process, stdout, stderr)."""
    python = tmp_path / "python"
    python.write_text(FAKE_PYTHON)
    python.chmod(0o755)
    assert " " not in str(tmp_path)
    program = _daemon()["ProgramArguments"]
    script = program[2].replace("/etc/webspec/gateway.env", str(env_file)).replace(
        "/opt/webspec/venv/bin/python", str(python))
    proc = subprocess.Popen([program[0], program[1], script, program[3]], env=_daemon()["EnvironmentVariables"],
                            cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = proc.communicate(timeout=30)
    return proc, out, err


def test_daemon_command_loads_gateway_env_and_becomes_the_gateway(tmp_path):
    env_file = tmp_path / "gateway.env"
    env_file.write_text("WEBSPEC_DOMAIN=example.com\nTOKEN='a b$c`d\\e\"f'\nPATH=\"$PATH:/usr/local/bin\"\n")
    proc, out, err = _run_daemon_command(tmp_path, env_file)
    assert proc.returncode == 0, err
    seen = dict(line.split("=", 1) for line in out.splitlines())
    # exec: the gateway keeps the PID that launchd supervises (and signals).
    assert seen["pid"] == str(proc.pid)
    assert seen["argv"] == "-I -m webspec"
    # set -a exports what gateway.env assigns, literally, and it overrides the plist's PATH.
    assert seen["TOKEN"] == "a b$c`d\\e\"f"
    assert seen["WEBSPEC_DOMAIN"] == "example.com"
    assert seen["WEBSPEC_LAUNCHD_SOCKET"] == "gateway"
    assert seen["PATH"] == SVC_PATH + ":/usr/local/bin"


@pytest.mark.parametrize("content", [None, "TOKEN=abc s3cr3tpart\nWEBSPEC_DOMAIN=example.com\n", "X=(\n"],
                         ids=["missing", "failing-line", "syntax-error"])
def test_daemon_command_does_not_start_the_gateway_without_a_good_gateway_env(tmp_path, content):
    env_file = tmp_path / "gateway.env"
    if content is not None:
        env_file.write_text(content)
    proc, out, _ = _run_daemon_command(tmp_path, env_file)
    assert proc.returncode != 0
    assert "argv=" not in out, "the gateway must not start half-configured"


def test_isolated_mode_keeps_the_working_directory_off_sys_path(tmp_path):
    """The attack -I closes: a module dropped in the working directory, which the stdio servers
    can write, runs inside the gateway when it starts."""
    interpreter_flags = shlex.split(CMD.split("; exec ")[1])[1:-2]
    assert interpreter_flags == ["-I"]
    marker = tmp_path / "shadowed"
    (tmp_path / "json.py").write_text(f"open({str(marker)!r}, 'w').close()\n")

    def run(*flags):
        subprocess.run([sys.executable, *flags, "-m", "json.tool"], cwd=tmp_path, input="{}",
                       capture_output=True, text=True, timeout=60)

    run()  # control: without -I the working directory comes first on sys.path
    assert marker.exists(), "the control run should have imported json.py from the working directory"
    marker.unlink()
    run(*interpreter_flags)
    assert not marker.exists()


def test_dev_launchagent_is_labelled_and_behaves_as_before():
    text = DEV_PLIST.read_text()
    assert "DEVELOPMENT" in text and "DP-1" in text and "DP-4" in text
    assert "gateway/deploy/macos" in text
    d = plistlib.loads(DEV_PLIST.read_bytes())
    assert d["Label"] == "com.webspec.gateway"
    assert d["ProgramArguments"] == ["/Users/preston/miniconda3/bin/python", "-m", "webspec"]
    assert d["WorkingDirectory"] == "/Users/preston/MCP/webspec-gateway"
    assert d["RunAtLoad"] is True and d["KeepAlive"] is True
    assert d["StandardOutPath"] == d["StandardErrorPath"] == "/Users/preston/.webspec/gateway.log"
    assert d["EnvironmentVariables"] == {
        "PATH": "/Users/preston/miniconda3/bin:/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": "/Users/preston/MCP/webspec-gateway",
    }


@pytest.mark.skipif(not shutil.which("plutil"), reason="plutil is macOS-only")
def test_plists_pass_plutil_lint():
    r = subprocess.run(["plutil", "-lint", str(DAEMON_PLIST), str(DEV_PLIST)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ── The installer: shape ──


def test_installer_is_executable_bash():
    assert os.access(INSTALL, os.X_OK)
    assert INSTALL.read_text().startswith("#!/bin/bash\n")
    r = subprocess.run([BASH, "-n", str(INSTALL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_installer_is_shellcheck_clean():
    r = subprocess.run(["shellcheck", str(INSTALL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_as_root_the_installer_restarts_in_a_clean_environment_before_running_anything():
    # sudo on macOS keeps the invoking user's PATH and HOME (sudo -E keeps everything), so a
    # directory on that PATH may be the agent's. The first statement after `set -euo pipefail`
    # must re-exec through /usr/bin/env -i with system directories only, and nothing above it may
    # run a command (no command substitution, no pipeline).
    lines = INSTALL.read_text().splitlines()
    code = [(i, ln) for i, ln in enumerate(lines) if ln.strip() and not ln.lstrip().startswith("#")]
    assert code[0][1] == "set -euo pipefail"
    start, first = code[1]
    assert first == 'if [ "$EUID" -eq 0 ] && [ "${BASH_SOURCE[0]}" = "$0" ]; then'
    end = next(i for i, ln in enumerate(lines) if i > start and ln == "fi")
    block = "\n".join(lines[start:end + 1])
    assert "exec /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME=/var/root" in block
    assert "/bin/bash \"$0\" \"$@\"" in block
    assert block.rstrip().endswith("PATH=/usr/bin:/bin:/usr/sbin:/sbin\n    HOME=/var/root\n    export PATH HOME\nfi")
    # The only command substitution before the reset is a bash builtin.
    assert block.count("$(") == 1 and "$(compgen -e)" in block
    assert not any("`" in ln for i, ln in code if i <= end)
    # Every option the header documents survives the reset, and nothing else does.
    documented = {"DRY_RUN", "WEBSPEC_DOMAIN", "PYTHON", "ALLOW_NONROOT_PYTHON", "ALLOW_NONROOT_SOURCE",
                  "ALLOW_NONROOT_PATH", "ALLOW_EXISTING_USER"}
    passed = set(re.findall(r'\$\{(\w+)\+"\1=\$\1"\}', block))
    assert passed == documented
    kept = re.search(r"case \$_name in\n\s+(.*?)\) ;;", block, re.S).group(1)
    assert {name.strip() for name in kept.replace("\\\n", " ").split("|")} == documented | {
        "PATH", "HOME", "PWD", "OLDPWD", "SHLVL", "_"}
    for name in documented:
        assert f"#   {name}=" in "\n".join(lines[:start])


# ── The installer: dry runs ──


FAKE_DSCL = r"""#!/bin/sh
# A local directory where 499 is a user's ID and 498 a group's, so the first ID free in both
# lists, counting down, is 497. Its login users are devuser (501) and opsadmin (502), and
# FAKE_DEVUSER_HOME, when set, is devuser's home. _webspec's records exist when these are set:
#   FAKE_WEBSPEC_ID=<n>  user and group, both with ID n
#   FAKE_USER_ID=<n|none>, FAKE_USER_GID=<n>, FAKE_GROUP_ID=<n|none>  one at a time ("none": the
#   record exists without its ID, as an interrupted run leaves it)
# Any other attribute of those records is FAKE_<attribute> (FAKE_UserShell, FAKE_GroupMembership,
# ...), missing when unset; FAKE_PRIMARY=<user> makes _webspec's group that user's primary group
# (FAKE_PRIMARY_GID=<n>: makes n that user's primary GID instead, whether or not a group has it).
# FAKE_MEMBER_OF=<group> lists _webspec among that group's members by name, FAKE_GUID_MEMBER_OF
# by its GeneratedUID.
printf 'dscl %s\n' "$*" >> "$FAKE_LOG"
uid=${FAKE_USER_ID:-}; ugid=${FAKE_USER_GID:-}; gid=${FAKE_GROUP_ID:-}
if [ -n "${FAKE_WEBSPEC_ID:-}" ]; then uid=$FAKE_WEBSPEC_ID; ugid=$FAKE_WEBSPEC_ID; gid=$FAKE_WEBSPEC_ID; fi
for attr in "$@"; do :; done
missing() { echo "<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)" >&2; exit 56; }
value() {
  eval "v=\${FAKE_$1-}"
  if [ -n "$v" ]; then echo "$1: $v"; else echo "No such key: $1"; fi
}
case "$*" in
  ". -list /Users UniqueID")
    printf '%s\n' "root 0" "nobody -2" "_www 70" "_taken 499" "devuser 501" "opsadmin 502"
    case $uid in ''|none) ;; *) echo "_webspec $uid" ;; esac ;;
  ". -list /Users PrimaryGroupID")
    printf '%s\n' "root 0" "_www 70" "_taken 20" "devuser 20" "opsadmin 20"
    if [ -n "$ugid" ]; then echo "_webspec $ugid"; fi
    if [ -n "${FAKE_PRIMARY:-}" ]; then echo "$FAKE_PRIMARY ${FAKE_PRIMARY_GID:-$gid}"; fi ;;
  ". -list /Groups PrimaryGroupID")
    printf '%s\n' "wheel 0" "staff 20" "_www 70" "_alsotaken 498"
    case $gid in ''|none) ;; *) echo "_webspec $gid" ;; esac ;;
  ". -list /Groups GroupMembership")
    printf '%s\n' "admin root devuser" "staff root"
    if [ -n "${FAKE_GroupMembership:-}" ]; then echo "_webspec $FAKE_GroupMembership"; fi
    if [ -n "${FAKE_MEMBER_OF:-}" ]; then echo "$FAKE_MEMBER_OF root _webspec"; fi ;;
  ". -list /Groups GroupMembers")
    printf '%s\n' "admin DDDD-ROOT EEEE-DEVUSER"
    if [ -n "${FAKE_GroupMembers:-}" ]; then echo "_webspec $FAKE_GroupMembers"; fi
    if [ -n "${FAKE_GUID_MEMBER_OF:-}" ]; then echo "$FAKE_GUID_MEMBER_OF DDDD-ROOT ${FAKE_GeneratedUID:-}"; fi ;;
  ". -read /Users/_webspec "*)
    [ -n "$uid" ] || missing
    case $attr in
      RecordName) echo "RecordName: _webspec" ;;
      UniqueID) if [ "$uid" = none ]; then echo "No such key: UniqueID"; else echo "UniqueID: $uid"; fi ;;
      PrimaryGroupID) if [ -z "$ugid" ]; then echo "No such key: PrimaryGroupID"; else echo "PrimaryGroupID: $ugid"; fi ;;
      *) value "$attr" ;;
    esac ;;
  ". -read /Groups/_webspec "*)
    [ -n "$gid" ] || missing
    case $attr in
      RecordName) echo "RecordName: _webspec" ;;
      PrimaryGroupID) if [ "$gid" = none ]; then echo "No such key: PrimaryGroupID"; else echo "PrimaryGroupID: $gid"; fi ;;
      *) value "$attr" ;;
    esac ;;
  ". -read /Users/devuser NFSHomeDirectory")
    if [ -z "${FAKE_DEVUSER_HOME:-}" ]; then missing; fi
    echo "NFSHomeDirectory: $FAKE_DEVUSER_HOME" ;;
  ". -read /Users/"*) missing ;;
  *) echo "fake dscl refuses: $*" >&2; exit 99 ;;
esac
"""

FAKE_LAUNCHCTL = r"""#!/bin/sh
# The development LaunchAgent is loaded in FAKE_DEV_DOMAIN (FAKE_DEV_AGENT=1: in gui/501). Nothing
# else is.
printf 'launchctl %s\n' "$*" >> "$FAKE_LOG"
domain=${FAKE_DEV_DOMAIN:-}
if [ "${FAKE_DEV_AGENT:-0}" = 1 ]; then domain=gui/501; fi
case "$*" in
  "print $domain/com.webspec.gateway")
    if [ -n "$domain" ]; then echo "$domain/com.webspec.gateway = {"; exit 0; fi
    exit 113 ;;
  "print "*) exit 113 ;;
  *) echo "fake launchctl refuses: $*" >&2; exit 99 ;;
esac
"""

# A dry run must not execute any of these.
MUTATING = ("sudo", "install", "chown", "chmod", "mkdir", "dscacheutil", "curl", "rm", "cp", "mv", "ln", "tee",
            "touch", "mktemp")
# Nor these, at all: as root, anything that runs before the clean environment comes from the
# invoking user's PATH, so the start of the script must not need them (finding: id, dirname and
# uname used to run there).
POISONED = ("id", "dirname", "basename", "uname")


def _write_tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def dry_run(tmp_path):
    """Run install.sh (from an unrelated directory) against fake directory services; return
    (result, calls)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    log.touch()
    _write_tool(bin_dir, "dscl", FAKE_DSCL)
    _write_tool(bin_dir, "launchctl", FAKE_LAUNCHCTL)
    for name in MUTATING + POISONED:
        _write_tool(bin_dir, name, f'#!/bin/sh\nprintf \'{name} %s\\n\' "$*" >> "$FAKE_LOG"\nexit 99\n')

    def run(*args, **env):
        full_env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(tmp_path),
            "FAKE_LOG": str(log),
            "DRY_RUN": "1",
            "PYTHON": sys.executable,
            "SUDO_USER": "devuser",
            "SUDO_UID": "501",
        }
        full_env.update(env)
        full_env = {k: v for k, v in full_env.items() if v is not None}
        result = subprocess.run([BASH, str(INSTALL), *args], env=full_env, cwd=tmp_path,
                                capture_output=True, text=True, timeout=120)
        return result, log.read_text().splitlines()

    return run


def _plan(result) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith("+ ")]


def _written(result, name: str) -> str:
    """The content a dry run shows for a file it would create (`+ cat > "$WORK/<name>"`)."""
    lines = result.stdout.splitlines()
    start = lines.index(f"+ cat > \"$WORK/{name}\" <<'EOF'")
    end = lines.index("EOF", start)
    return "\n".join(lines[start + 1:end]) + "\n"


def _next_steps(result) -> str:
    return result.stdout.split("== Next steps\n", 1)[1]


def _assert_only_probes_ran(calls: list[str]) -> None:
    for call in calls:
        assert call.startswith(("dscl . -read ", "dscl . -list ", "launchctl print ")), f"dry run executed: {call}"


@non_root
@fresh_host
def test_dry_run_plans_a_fresh_install(dry_run, tmp_path):
    result, calls = dry_run(FAKE_DEV_AGENT="0", WEBSPEC_DOMAIN="example.com")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    plan = _plan(result)
    # install.sh quotes words exactly as shlex.quote does (same safe characters).
    gateway_src = shlex.quote(str(GATEWAY))
    example = shlex.quote(str(EXAMPLE))
    plist_src = shlex.quote(str(DAEMON_PLIST))
    expected = [
        # Hidden system account on the highest ID free as both a UID and a GID.
        "+ dscl . -create /Groups/_webspec",
        "+ dscl . -create /Groups/_webspec PrimaryGroupID 497",
        "+ dscl . -create /Users/_webspec",
        "+ dscl . -create /Users/_webspec UniqueID 497",
        "+ dscl . -create /Users/_webspec PrimaryGroupID 497",
        "+ dscl . -create /Users/_webspec Password '*'",
        "+ dscl . -create /Users/_webspec UserShell /usr/bin/false",
        "+ dscl . -create /Users/_webspec NFSHomeDirectory /var/empty",
        "+ dscl . -create /Users/_webspec IsHidden 1",
        # Configuration: root-owned, readable by the service group only (DP-4).
        "+ chown root:_webspec /etc/webspec",
        "+ chmod 0750 /etc/webspec",
        '+ install -m 0640 -o root -g _webspec "$WORK/config.json" /etc/webspec/config.json',
        f"+ install -m 0644 -o root -g wheel {example} /etc/webspec/config.example.json",
        '+ install -m 0640 -o root -g _webspec "$WORK/gateway.env" /etc/webspec/gateway.env',
        # The gateway reads the key through its group; only root can change it (finding: the
        # service user owned the key and could rewrite it).
        "+ install -m 0440 -o root -g _webspec /dev/null /etc/webspec/guard.key",
        '+ install -m 0644 -o root -g wheel "$WORK/allowed_signers" /etc/webspec/allowed_signers',
        # State and logs: the service user's alone.
        "+ chown _webspec:_webspec /var/lib/webspec",
        "+ chmod 0700 /var/lib/webspec",
        "+ chown _webspec:_webspec /var/log/webspec",
        "+ chmod 0750 /var/log/webspec",
        # Code: a fresh root-owned venv with this checkout installed, importable by the service user.
        "+ chown root:wheel /opt/webspec",
        "+ chmod 0755 /opt/webspec",
        "+ rm -rf /opt/webspec/venv",
        "+ /opt/webspec/venv/bin/python -I -m pip --isolated install --no-cache-dir "
        f"--disable-pip-version-check --progress-bar off --upgrade {gateway_src}",
        "+ chown -R root:wheel /opt/webspec/venv",
        "+ chmod -R go-w /opt/webspec/venv",
        "+ sudo -u _webspec /usr/bin/env -i HOME=/var/lib/webspec PATH=/usr/bin:/bin "
        "/opt/webspec/venv/bin/python -I -c 'import webspec.app'",
        # The daemon, installed unchanged: site settings live in gateway.env.
        f"+ install -m 0644 -o root -g wheel {plist_src} {INSTALLED_PLIST}",
        f"+ plutil -lint {INSTALLED_PLIST}",
    ]
    for line in expected:
        assert line in plan, f"missing from the plan: {line}\n" + "\n".join(plan)
    venv = next(i for i, line in enumerate(plan) if line.endswith(" -I -m venv /opt/webspec/venv"))
    assert plan.index("+ rm -rf /opt/webspec/venv") < venv < next(i for i, line in enumerate(plan) if "-m pip" in line)
    assert not any("plutil -replace" in line or "plutil -insert" in line for line in plan)
    # The example's servers never go live: config.json is created with none.
    assert not any(line.startswith("+ install ") and line.endswith(" /etc/webspec/config.json") and example in line
                   for line in plan)
    # The account exists before anything is handed to it, and the code before the daemon.
    first_use = min(i for i, line in enumerate(plan) if "_webspec" in line and not line.startswith("+ dscl"))
    assert max(i for i, line in enumerate(plan) if line.startswith("+ dscl")) < first_use
    assert plan.index(f"+ install -m 0644 -o root -g wheel {plist_src} {INSTALLED_PLIST}") > max(
        i for i, line in enumerate(plan) if "-m pip" in line)
    # guard.key is created empty, so the daemon is not loaded, and the plist just installed is
    # disabled so that launchd does not load it at the next boot either (finding: "not loading"
    # lasted until the next boot). The operator is told how to fill the key.
    assert [line for line in plan if line.startswith("+ launchctl")] == [
        "+ launchctl disable system/com.webspec.gateway.daemon"]
    assert plan.index("+ launchctl disable system/com.webspec.gateway.daemon") > plan.index(
        f"+ install -m 0644 -o root -g wheel {plist_src} {INSTALLED_PLIST}")
    assert "not loading: /etc/webspec/guard.key is empty" in result.stdout
    assert "stays disabled, at boot too, until a run of this installer passes these checks" in result.stdout
    assert "WEBSPEC_GUARD_KEY=" not in result.stdout + result.stderr
    if _owned_by_others(GATEWAY):
        # Root would build this checkout: a real run refuses it (finding: agent-writable source).
        assert "a real run refuses" in result.stderr and "ALLOW_NONROOT_SOURCE=1" in result.stderr

    config = _written(result, "config.json")
    assert json.loads(config)["mcpServers"] == {}
    (tmp_path / "config.json").write_text(config)
    assert parse_claude_config(tmp_path / "config.json") == {}

    env = _written(result, "gateway.env")
    assert [ln for ln in env.splitlines() if ln and not ln.startswith("#")] == ["WEBSPEC_DOMAIN=example.com"]
    (tmp_path / "gateway.env").write_text(env)
    loaded = subprocess.run(["/bin/sh", "-c", 'set -ae; . "$1"; printf "%s|%s" "$WEBSPEC_DOMAIN" "${WEBSPEC_GUARD_KEY-unset}"',
                             "sh", str(tmp_path / "gateway.env")], capture_output=True, text=True, timeout=30)
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout == "example.com|unset"

    signers = _written(result, "allowed_signers")
    assert all(ln.startswith("#") for ln in signers.splitlines())
    assert "503 approval_unavailable" in signers


@non_root
@pytest.mark.skipif(_guard_key_readable_here(), reason="needs a Mac where WebSpec is installed")
def test_a_dry_run_without_root_stops_at_an_installed_guard_key(dry_run):
    # What the fresh_host tests cannot show here: the dry run stops where root would be needed.
    result, calls = dry_run(FAKE_DEV_AGENT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    flat = " ".join(result.stdout.split())
    assert ("/etc/webspec/guard.key cannot be read without root, and what a real run does from here depends "
            "on it and on checks that need root as well: run the dry run with sudo to see that part of the "
            "plan.") in flat
    assert not [line for line in _plan(result) if line.startswith("+ launchctl")]


FILL_COMMAND = f"/usr/local/bin/op read 'op://<vault>/<item>/<field>' | sudo {INSTALL} --fill-key"


@non_root
@fresh_host
def test_dry_run_tells_the_operator_safe_ways_to_edit_and_fill_secrets(dry_run, tmp_path):
    result, _ = dry_run(FAKE_DEV_AGENT="0")
    assert result.returncode == 0, result.stderr
    steps = _next_steps(result)
    flat = " ".join(steps.split())
    # The key is filled by the installer itself, which replaces guard.key atomically and only
    # with a key the gateway accepts; never with tee, which empties the file before the password
    # manager answers (finding: a failed fill emptied the key of a running gateway, which reads it
    # on every request), nor with a script that keeps the first line of what it reads.
    assert "tee" not in steps
    assert f"      {FILL_COMMAND}\n" in steps
    assert "only with a key that the installed gateway accepts" in flat
    assert "Anything else, a failed read included, leaves the old key in place" in flat
    assert "then run this installer again to (re)start the daemon" in flat
    assert "sudo -H /usr/bin/vi /etc/webspec/config.json" in steps
    assert "Never with sudo -e (sudoedit)" in steps
    assert "never in args" in steps
    assert "503 approval_unavailable (AP-7)" in steps
    # DP-5 and DP-6 are the operator's on macOS: no proxy is shipped, and webspec-ctl's Caddy
    # management needs systemd (finding: nothing said so).
    assert "These macOS files alone do not meet DP-5 and DP-6: this installer ships no proxy" in flat
    assert "webspec-ctl manages Caddy (setup-caddy.sh, and the site blocks and reloads of webspec-ctl add) " \
           "only on Linux with systemd, so do not use those parts of it here" in flat
    assert "listening on loopback only, on 127.0.0.1:7001 and [::1]:7001" in flat
    assert ("forward only hosts under your domain to 127.0.0.1:7002, with the Host header kept, and *.localhost "
            "names only for connections from this host, answering 421 to one that carries Cloudflare's Cf-Ray "
            "header (DP-5)") in flat
    assert "keep query strings, request and response headers, and userinfo out of its logs (DP-6)" in flat
    # Every command the operator is told to run with sudo names its program by absolute path:
    # sudo would look a bare name up on the operator's PATH (macOS keeps it).
    checked = 0
    for line in steps.splitlines():
        if not line.startswith("      ") or "sudo" not in line:
            continue
        for match in re.finditer(r"\bsudo((?:\s+-u\s+\S+|\s+-[A-Za-z]+)*)\s+(\S+)", line):
            assert match.group(2).startswith("/"), f"sudo runs a bare name: {line}"
            checked += 1
    assert checked >= 6


@non_root
def test_dry_run_reuses_an_existing_account(dry_run):
    result, calls = dry_run(FAKE_WEBSPEC_ID="450", FAKE_DEV_AGENT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    plan = _plan(result)
    assert "user _webspec exists (UID 450)" in result.stdout
    assert not any("UniqueID" in line or "PrimaryGroupID" in line for line in plan)
    assert not any(line in ("+ dscl . -create /Users/_webspec", "+ dscl . -create /Groups/_webspec") for line in plan)
    # Password, shell, home and the hidden flag are re-applied on every run.
    assert "+ dscl . -create /Users/_webspec Password '*'" in plan
    assert "+ dscl . -create /Users/_webspec UserShell /usr/bin/false" in plan
    assert "+ dscl . -create /Users/_webspec IsHidden 1" in plan
    assert "development LaunchAgent" not in result.stderr
    assert "existing _webspec" not in result.stderr


# What an account this installer made looks like, and what a login account looks like instead.
SERVICE_ACCOUNT = {"FAKE_WEBSPEC_ID": "450", "FAKE_UserShell": "/usr/bin/false", "FAKE_NFSHomeDirectory": "/var/empty",
                   "FAKE_IsHidden": "1", "FAKE_GeneratedUID": "AAAA-SELF", "FAKE_GroupMembership": "_webspec",
                   "FAKE_GroupMembers": "AAAA-SELF"}
LOGIN_LIKE = [
    ({"FAKE_WEBSPEC_ID": "501"}, "its UID, 501, is in the range of login users (500 and up)"),
    ({"FAKE_UserShell": "/bin/zsh"}, "its login shell is /bin/zsh"),
    ({"FAKE_NFSHomeDirectory": "/Users/webspec"}, "its home is /Users/webspec"),
    ({"FAKE_IsHidden": "0"}, "it is shown at login (IsHidden 0)"),
    ({"FAKE_AuthenticationAuthority": ";ShadowHash;HASHLIST:<SALTED-SHA512-PBKDF2> ;SecureToken;"},
     "it has a password (AuthenticationAuthority)"),
    ({"FAKE_GroupMembership": "_webspec devuser"}, "its group _webspec has the members devuser"),
    ({"FAKE_GroupMembers": "AAAA-SELF BBBB-OTHER"}, "its group _webspec has the members BBBB-OTHER (GUIDs)"),
    ({"FAKE_NestedGroups": "CCCC-GROUP"}, "its group _webspec has nested groups"),
    ({"FAKE_PRIMARY": "devuser"}, "its group _webspec is the primary group of devuser"),
    # Review: an account sharing IDs or groups with others was adopted. As root (UID 0), or as
    # another service, the gateway runs with that account's rights; members of another group
    # that has _webspec's GID read guard.key; groups _webspec is in are the gateway's too.
    ({"FAKE_WEBSPEC_ID": "0"}, "its UID, 0, is also the UID of root"),
    ({"FAKE_WEBSPEC_ID": "70"}, "its UID, 70, is also the UID of _www"),
    ({"FAKE_WEBSPEC_ID": "150"}, "its UID, 150, is below 200: this installer gives service accounts 200-499"),
    ({"FAKE_MEMBER_OF": "admin"}, "it is a member of the groups admin"),
    ({"FAKE_GUID_MEMBER_OF": "_lpadmin"}, "it is a member of the groups _lpadmin"),
]
LOGIN_LIKE_IDS = ["uid", "shell", "home", "shown", "password", "member", "member-guid", "nested", "primary-group",
                  "root-uid", "shared-uid", "low-uid", "in-other-group", "in-other-group-by-guid"]


@pytest.mark.parametrize("attrs", [{}, {"FAKE_NFSHomeDirectory": "/var/lib/webspec"}, {"FAKE_UserShell": "/sbin/nologin"}],
                         ids=["as-installed", "home-in-state-dir", "nologin"])
def test_an_existing_service_account_is_adopted(tmp_path, attrs):
    result, _ = _source(tmp_path, 'check_account 450 450; echo adopted', {"dscl": FAKE_DSCL},
                        **{**SERVICE_ACCOUNT, **attrs})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "adopted" and result.stderr == ""


@pytest.mark.parametrize("attrs, problem", LOGIN_LIKE, ids=LOGIN_LIKE_IDS)
def test_real_run_refuses_to_adopt_a_login_like_account(tmp_path, attrs, problem):
    # DP-1 (finding: an existing login account was adopted as the service user): whoever can log
    # in as _webspec, or is in its group, can read guard.key and gateway.env.
    env = {**SERVICE_ACCOUNT, **attrs}
    uid = env["FAKE_WEBSPEC_ID"]
    result, _ = _source(tmp_path, f'check_account {uid} {uid}; echo adopted', {"dscl": FAKE_DSCL}, **env)
    assert result.returncode == 1 and "adopted" not in result.stdout
    assert f"existing _webspec: {problem}" in result.stderr
    assert "refusing to adopt the existing _webspec" in result.stderr
    assert "ALLOW_EXISTING_USER=1" in result.stderr
    # With the override it is adopted, and the warning says what this run cannot undo.
    result, _ = _source(tmp_path, f'check_account {uid} {uid}; echo adopted', {"dscl": FAKE_DSCL},
                        ALLOW_EXISTING_USER="1", **env)
    assert result.returncode == 0 and result.stdout.strip() == "adopted"
    assert f"existing _webspec: {problem}" in result.stderr
    assert "Adopting it anyway because ALLOW_EXISTING_USER=1" in result.stderr
    assert "but not a password, the group's other members, who can read" in _warnings(result.stderr)
    assert "IDs it shares with other accounts, or its other groups" in _warnings(result.stderr)


@pytest.mark.parametrize("gid, problem", [
    ("498", "its group's GID, 498, is also the GID of the group _alsotaken"),
    ("20", "its group's GID, 20, is also the GID of the group staff"),
    ("0", "its group's GID, 0, is outside 200-499, where this installer makes it"),
    ("600", "its group's GID, 600, is outside 200-499, where this installer makes it"),
], ids=["shared-gid", "staff-gid", "wheel-gid", "high-gid"])
def test_real_run_refuses_a_service_group_that_shares_its_id(tmp_path, gid, problem):
    # Files of _webspec's group (guard.key, gateway.env) are readable to every member of a group
    # with the same GID: staff, every login user's primary group, would read the key.
    env = {**SERVICE_ACCOUNT, "FAKE_USER_ID": "450", "FAKE_USER_GID": gid, "FAKE_GROUP_ID": gid}
    del env["FAKE_WEBSPEC_ID"]
    result, _ = _source(tmp_path, f'check_account 450 {gid}; echo adopted', {"dscl": FAKE_DSCL}, **env)
    assert result.returncode == 1 and "adopted" not in result.stdout
    assert f"existing _webspec: {problem}" in result.stderr


def test_the_refusal_says_how_to_take_the_account_out_of_its_other_groups(tmp_path):
    # Groups list members by name: a fresh _webspec would inherit the memberships of a deleted one.
    result, _ = _source(tmp_path, 'check_account 450 450; echo adopted', {"dscl": FAKE_DSCL},
                        **{**SERVICE_ACCOUNT, "FAKE_MEMBER_OF": "admin"})
    assert result.returncode == 1
    assert "sudo /usr/sbin/dseditgroup -o edit -d _webspec -t user <group>" in result.stderr


@non_root
def test_dry_run_names_a_login_like_account_that_a_real_run_refuses(dry_run):
    result, calls = dry_run(FAKE_DEV_AGENT="0", **{**SERVICE_ACCOUNT, "FAKE_UserShell": "/bin/zsh",
                                                   "FAKE_AuthenticationAuthority": ";ShadowHash;"})
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    assert "existing _webspec: its login shell is /bin/zsh" in result.stderr
    assert "existing _webspec: it has a password (AuthenticationAuthority)" in result.stderr
    assert "A real run refuses to adopt this _webspec unless ALLOW_EXISTING_USER=1." in result.stderr


def test_a_real_run_refuses_before_it_changes_the_account(tmp_path):
    # converge_account would take away the shell and the home, hiding what made it a login account.
    result, calls = _source(tmp_path, "ensure_account; echo done", {"dscl": FAKE_DSCL},
                            **{**SERVICE_ACCOUNT, "FAKE_UserShell": "/bin/bash"})
    assert result.returncode == 1 and "done" not in result.stdout
    assert "existing _webspec: its login shell is /bin/bash" in result.stderr
    assert not any(" -create " in call or " -delete " in call for call in calls)


@non_root
def test_dry_run_finishes_a_user_record_that_an_interrupted_run_left_without_ids(dry_run):
    result, calls = dry_run(FAKE_USER_ID="none", FAKE_GROUP_ID="450", FAKE_DEV_AGENT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    plan = _plan(result)
    assert "user _webspec exists without a UniqueID (an interrupted run?): giving it 450" in result.stdout
    assert "+ dscl . -create /Users/_webspec UniqueID 450" in plan
    assert "+ dscl . -create /Users/_webspec PrimaryGroupID 450" in plan
    assert "+ dscl . -create /Users/_webspec" not in plan and "+ dscl . -create /Groups/_webspec" not in plan
    assert not any("/Groups/_webspec PrimaryGroupID" in line for line in plan)


@non_root
def test_dry_run_gives_the_service_user_its_group_as_primary_group(dry_run):
    # guard.key is readable through the group only (root:_webspec 0440), and check_guard_key's
    # probe runs with the user's own groups, as `sudo -u` gives them.
    result, calls = dry_run(FAKE_USER_ID="450", FAKE_USER_GID="20", FAKE_GROUP_ID="451", FAKE_DEV_AGENT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    assert "_webspec's primary group is 20, not _webspec (451): making it _webspec" in result.stdout
    plan = _plan(result)
    # The cache flushed after, so that the key probe's `sudo -u` sees the new group.
    assert plan.index("+ dscl . -create /Users/_webspec PrimaryGroupID 451") + 1 == plan.index("+ dscacheutil -flushcache")


@non_root
def test_dry_run_finishes_a_group_record_that_an_interrupted_run_left_without_its_id(dry_run):
    result, calls = dry_run(FAKE_GROUP_ID="none", FAKE_DEV_AGENT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    plan = _plan(result)
    assert "group _webspec exists without a PrimaryGroupID (an interrupted run?): giving it 497" in result.stdout
    assert "+ dscl . -create /Groups/_webspec" not in plan
    assert "+ dscl . -create /Groups/_webspec PrimaryGroupID 497" in plan
    assert "+ dscl . -create /Users/_webspec UniqueID 497" in plan


def _next_run(tmp_path, uid: str, gid: str, **env) -> subprocess.CompletedProcess:
    """check_account as the next run calls it, on the account as a run leaves it: UID uid, GID gid
    for its group and its primary group, and what converge_account sets."""
    state = {**SERVICE_ACCOUNT, "FAKE_USER_ID": uid, "FAKE_USER_GID": gid, "FAKE_GROUP_ID": gid, **env}
    del state["FAKE_WEBSPEC_ID"]
    result, _ = _source(tmp_path, f"check_account {uid} {gid}; echo adopted", {"dscl": FAKE_DSCL}, **state)
    return result


@non_root
@pytest.mark.parametrize("user_gid, others, gid", [
    ("450", {}, "450"),
    ("200", {}, "200"),
    ("499", {}, "499"),
    ("600", {}, "497"),
    ("500", {}, "497"),
    ("150", {}, "497"),
    ("199", {}, "497"),
    ("498", {}, "497"),
    ("450", {"FAKE_PRIMARY": "devuser", "FAKE_PRIMARY_GID": "450"}, "497"),
    ("450", {"FAKE_PRIMARY": "devuser", "FAKE_PRIMARY_GID": "0450"}, "497"),
    ("0450", {"FAKE_PRIMARY": "devuser", "FAKE_PRIMARY_GID": "450"}, "497"),
], ids=["in-range", "lowest", "highest", "above-range", "just-above", "below-range", "just-below", "another-groups-gid",
        "another-users-primary-gid", "another-users-primary-gid-zero-padded", "zero-padded"])
def test_dry_run_makes_the_missing_group_with_a_gid_the_next_run_adopts(dry_run, tmp_path, user_gid, others, gid):
    # Review: a run that found _webspec without its group made the group with the GID the user
    # named, 600, and the next run refused the account (its group's GID is outside 200-499). The
    # group takes that GID only if the next run accepts it, else a free one, and the user's
    # primary group moves to it, as it does when another group has the GID. The next run reads
    # IDs as numbers: 0450 is 450, in _webspec's record or devuser's. The run says why it passes
    # over the user's GID.
    result, calls = dry_run(FAKE_USER_ID="450", FAKE_USER_GID=user_gid, FAKE_DEV_AGENT="0", **others)
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    assert "existing _webspec" not in result.stderr
    plan = _plan(result)
    assert "+ dscl . -create /Groups/_webspec" in plan
    assert [line for line in plan if "/Groups/_webspec PrimaryGroupID" in line] == [
        f"+ dscl . -create /Groups/_webspec PrimaryGroupID {gid}"]
    moved = [line for line in plan if "/Users/_webspec PrimaryGroupID" in line]
    if gid == user_gid:
        assert moved == []
        assert "primary GID" not in result.stdout
    else:
        assert (f"_webspec's primary GID, {user_gid}, is not a free ID in 200-499: giving group _webspec {gid} instead"
                in result.stdout)
        assert f"_webspec's primary group is {user_gid}, not _webspec ({gid}): making it _webspec" in result.stdout
        assert moved == [f"+ dscl . -create /Users/_webspec PrimaryGroupID {gid}"]
    result = _next_run(tmp_path, "450", gid, **others)
    assert result.returncode == 0 and result.stdout.strip() == "adopted", result.stderr
    assert result.stderr == ""


@non_root
@pytest.mark.parametrize("user_gid, gid, uid", [
    ("450", "450", "450"),
    ("600", "497", "497"),
    ("499", "499", "497"),
    ("0450", "497", "497"),
], ids=["in-range", "above-range", "gid-is-another-users-uid", "zero-padded"])
def test_dry_run_gives_ids_in_range_to_a_user_left_without_its_uid_and_group(dry_run, tmp_path, user_gid, gid, uid):
    # Review: the UID followed the GID that the new group took from the user, 600 as well: a login
    # user's UID, which the next run refuses too. The UID takes the group's number only if no other
    # user has it (499 is _taken's), and neither ID is written 0450: a real run checks the UID it
    # gives against id -u, which prints 450.
    result, calls = dry_run(FAKE_USER_ID="none", FAKE_USER_GID=user_gid, FAKE_DEV_AGENT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    plan = _plan(result)
    assert f"+ dscl . -create /Groups/_webspec PrimaryGroupID {gid}" in plan
    assert f"user _webspec exists without a UniqueID (an interrupted run?): giving it {uid}" in result.stdout
    assert [line for line in plan if "UniqueID" in line] == [f"+ dscl . -create /Users/_webspec UniqueID {uid}"]
    result = _next_run(tmp_path, uid, gid)
    assert result.returncode == 0 and result.stdout.strip() == "adopted", result.stderr
    assert result.stderr == ""


@non_root
def test_dry_run_gives_a_uid_in_range_beside_an_adopted_group_outside_it(dry_run, tmp_path):
    # ALLOW_EXISTING_USER=1 adopts a _webspec group whose GID, 600, is outside 200-499. The UID
    # that this run gives the user is still one in the range, not the group's 600, a login user's.
    result, calls = dry_run(FAKE_USER_ID="none", FAKE_GROUP_ID="600", ALLOW_EXISTING_USER="1", FAKE_DEV_AGENT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    assert "group _webspec exists (GID 600)" in result.stdout
    assert "user _webspec exists without a UniqueID (an interrupted run?): giving it 497" in result.stdout
    assert [line for line in _plan(result) if "UniqueID" in line] == ["+ dscl . -create /Users/_webspec UniqueID 497"]
    # The next run names the group's GID again, which the operator adopted, and nothing this run made.
    result = _next_run(tmp_path, "497", "600", ALLOW_EXISTING_USER="1")
    assert result.returncode == 0 and result.stdout.strip() == "adopted", result.stderr
    assert [line for line in result.stderr.splitlines() if line.startswith("WARNING: existing _webspec: ")] == [
        "WARNING: existing _webspec: its group's GID, 600, is outside 200-499, where this installer makes it"]


@non_root
@pytest.mark.parametrize("primary_gid", ["497", "0497"], ids=["plain", "zero-padded"])
def test_dry_run_gives_a_new_group_no_gid_that_is_another_users_primary_group(dry_run, tmp_path, primary_gid):
    # A user whose primary GID names no group would be in a group made with that GID, and read
    # guard.key; the next run refuses the account (its group is that user's primary group), and
    # reads 0497 as 497.
    result, calls = dry_run(FAKE_PRIMARY="devuser", FAKE_PRIMARY_GID=primary_gid, FAKE_DEV_AGENT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    plan = _plan(result)
    assert "+ dscl . -create /Groups/_webspec PrimaryGroupID 496" in plan
    assert "+ dscl . -create /Users/_webspec UniqueID 496" in plan
    # No _webspec user names a GID for the run to pass over.
    assert "primary GID" not in result.stdout
    result = _next_run(tmp_path, "496", "496", FAKE_PRIMARY="devuser", FAKE_PRIMARY_GID=primary_gid)
    assert result.returncode == 0 and result.stdout.strip() == "adopted", result.stderr
    assert result.stderr == ""


def test_an_id_counts_as_used_when_dscl_fails_to_list_the_ids(tmp_path):
    # id_unused pipes dscl's listing into awk: pipefail keeps a dscl that fails partway from
    # passing for a listing in which no record has the ID.
    failing = "#!/bin/sh\necho 'root 0'\nexit 1\n"
    result, _ = _source(tmp_path, "if id_unused 450 Users UniqueID; then echo unused; else echo used; fi",
                        {"dscl": failing})
    assert result.stdout.strip() == "used", result.stderr


@non_root
@fresh_host
@pytest.mark.parametrize("sudo_user, sudo_uid", [("devuser", "501"), ("opsadmin", "502"), (None, None)],
                         ids=["agent-user-runs-sudo", "separate-admin-runs-sudo", "no-sudo"])
def test_dry_run_warns_about_a_loaded_dev_agent_and_does_not_load(dry_run, sudo_user, sudo_uid):
    # Every login user is checked, not only the one who ran sudo: the header recommends running
    # the installer from an admin account the agent never uses (finding: that hid the agent).
    result, calls = dry_run(FAKE_DEV_AGENT="1", SUDO_USER=sudo_user, SUDO_UID=sudo_uid)
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_only_probes_ran(calls)
    assert "dscl . -list /Users UniqueID" in calls
    for uid in ("501", "502"):
        assert f"launchctl print gui/{uid}/com.webspec.gateway" in calls
    assert "development LaunchAgent com.webspec.gateway is loaded for devuser (gui/501)" in result.stderr
    assert "/bin/launchctl bootout gui/501/com.webspec.gateway" in result.stderr
    assert "does not (re)start com.webspec.gateway.daemon" in result.stderr
    # A fresh install's own reason comes first (its guard.key is empty); the agent is named too.
    assert "not loading: /etc/webspec/guard.key is empty" in result.stdout
    assert [line for line in _plan(result) if line.startswith("+ launchctl")] == [
        "+ launchctl disable system/com.webspec.gateway.daemon"]
    assert "      /bin/launchctl bootout gui/501/com.webspec.gateway && " \
           "/bin/launchctl disable gui/501/com.webspec.gateway" in _next_steps(result)


@non_root
def test_dry_run_finds_a_dev_agent_in_a_user_domain(dry_run):
    result, _ = dry_run(FAKE_DEV_DOMAIN="user/502")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "development LaunchAgent com.webspec.gateway is loaded for opsadmin (user/502)" in result.stderr
    assert "/bin/launchctl bootout user/502/com.webspec.gateway && " \
           "/bin/launchctl disable user/502/com.webspec.gateway" in _next_steps(result)


def test_a_dev_agent_plist_in_a_login_users_home_is_reported(tmp_path):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    plist = home / "Library" / "LaunchAgents" / "com.webspec.gateway.plist"
    plist.write_text("<plist/>")
    result, calls = _source(tmp_path, 'check_dev_agent; echo "agents=[$DEV_AGENT]"',
                            {"dscl": FAKE_DSCL, "launchctl": FAKE_LAUNCHCTL}, FAKE_DEVUSER_HOME=str(home))
    assert result.returncode == 0, result.stderr
    # Not loaded now, so not a reason to hold back the daemon, but it loads at devuser's next login.
    assert result.stdout.strip() == "agents=[]"
    assert f"{plist} loads the development gateway at devuser's next login" in result.stderr
    assert "/bin/launchctl disable gui/501/com.webspec.gateway (as devuser)" in result.stderr
    assert "dscl . -read /Users/opsadmin NFSHomeDirectory" in calls


@non_root
def test_dry_run_option_is_the_same_as_dry_run_1(dry_run):
    result, calls = dry_run("--dry-run", DRY_RUN=None, FAKE_DEV_AGENT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("DRY RUN:")
    _assert_only_probes_ran(calls)


def test_help_prints_usage_and_changes_nothing(dry_run):
    result, calls = dry_run("--help", DRY_RUN=None)
    assert result.returncode == 0
    assert result.stdout.startswith("Usage: sudo ") and "--dry-run" in result.stdout
    assert f"<password manager> | sudo {INSTALL} --fill-key" in result.stdout
    assert calls == []


@pytest.mark.parametrize("arg", ["--dryrun", "-y", "install"])
def test_unknown_arguments_stop_before_anything_runs(dry_run, arg):
    # `sudo install.sh --dryrun` must not turn into a real install.
    result, calls = dry_run(arg, DRY_RUN=None)
    assert result.returncode == 2
    assert f"unknown argument: {arg}" in result.stderr
    assert "must be run as root" not in result.stderr
    assert calls == []


def test_dry_run_rejects_a_bad_domain(dry_run):
    result, calls = dry_run(WEBSPEC_DOMAIN="Example.COM/evil")
    assert result.returncode != 0
    assert "not a lowercase DNS name" in result.stderr
    assert calls == []


@non_root
def test_dry_run_rejects_a_python_that_is_not_311_or_newer(dry_run, tmp_path):
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    result, _ = dry_run(PYTHON=str(fake))
    assert result.returncode != 0
    assert f"PYTHON={fake} is not an executable Python 3.11 or newer" in result.stderr


def test_python_must_be_an_absolute_path(dry_run):
    result, calls = dry_run(PYTHON="python3")
    assert result.returncode != 0
    assert "PYTHON=python3 must be an absolute path" in result.stderr
    assert calls == []


@non_root
def test_real_run_refuses_without_root(dry_run):
    result, calls = dry_run(DRY_RUN="0")
    assert result.returncode != 0
    assert "must be run as root" in result.stderr
    assert calls == []  # the poisoned id, dirname and uname never ran


# ── The installer: its checks, sourced ──


FAKE_SUDO = """#!/bin/sh
printf 'sudo %s\\n' "$*" >> "$FAKE_LOG"
if [ "$1" = -u ]; then shift 2; fi
exec "$@"
"""

FAKE_INSTALL = """#!/bin/sh
printf 'install %s\\n' "$*" >> "$FAKE_LOG"
for a; do src=$dst; dst=$a; done
cp "$src" "$dst"
"""

# FAKE_LSOF_<port>: who listens on that port, as words PID or PID@address (127.0.0.1:<port> when
# no address is given). -t prints the PIDs, -F pn lsof's p and n lines.
FAKE_LSOF = """#!/bin/sh
fmt=t
for a; do case $a in -iTCP:*) port=${a#-iTCP:} ;; -F) fmt=F ;; esac; done
eval "items=\\${FAKE_LSOF_$port:-}"
[ -n "$items" ] || exit 1
for i in $items; do
  pid=${i%%@*}
  case $i in *@*) addr=${i#*@} ;; *) addr=127.0.0.1:$port ;; esac
  if [ "$fmt" = t ]; then echo "$pid"; else printf 'p%s\\nf3\\nn%s\\n' "$pid" "$addr"; fi
done
"""

FAKE_PS = """#!/bin/sh
case "$2" in
  user=) echo "${FAKE_PS_USER:-someone}" ;;
  comm=) echo python3.12 ;;
  command=) echo "${FAKE_PS_COMMAND:-/usr/local/bin/caddy run --config /etc/caddy/Caddyfile}" ;;
  uid=) echo "${FAKE_PS_UID:-250}" ;;
esac
"""

FAKE_LAUNCHCTL_LIST = """#!/bin/sh
printf 'launchctl %s\\n' "$*" >> "$FAKE_LOG"
[ "$1" = list ] || exit 113
printf 'PID\\tStatus\\tLabel\\n%s\\t0\\tcom.apple.example\\n%s\\t0\\tcom.webspec.gateway.daemon\\n' 77 "${FAKE_DAEMON_PID:--}"
"""

FAKE_CURL = """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$FAKE_LOG"
printf '%s' "${FAKE_HTTP_CODE:-200}"
"""

FAKE_PLUTIL = """#!/bin/sh
printf 'plutil %s\\n' "$*" >> "$FAKE_LOG"
"""


def _source(tmp_path: Path, body: str, tools: dict[str, str] | None = None, **env) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Source install.sh, which defines its functions and runs nothing, then run body with fake
    tools first on PATH; returns (result, calls)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    log.touch()
    for name, text in (tools or {}).items():
        _write_tool(bin_dir, name, text)
    full_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path),
                "FAKE_LOG": str(log), "DRY_RUN": "0", **env}
    result = subprocess.run([BASH, "-c", '. "$1"; ' + body, "bash", str(INSTALL)], env=full_env,
                            capture_output=True, text=True, timeout=120)
    return result, log.read_text().splitlines()


@non_root
@pytest.mark.skipif(not _owned_by_others(GATEWAY), reason="this checkout is root-owned")
def test_real_run_refuses_a_checkout_that_others_can_modify(tmp_path):
    result, _ = _source(tmp_path, "check_source; echo passed")
    assert result.returncode == 1
    assert "passed" not in result.stdout
    assert f"refusing to install from {GATEWAY}" in result.stderr
    assert "sudo -H /usr/bin/git clone https://github.com/I-m-A-g-I-n-E/WebSpec.git /opt/webspec/src" in result.stderr
    assert "ALLOW_NONROOT_SOURCE=1" in result.stderr


@non_root
@pytest.mark.skipif(not _owned_by_others(GATEWAY), reason="this checkout is root-owned")
def test_allow_nonroot_source_accepts_the_checkout_and_names_the_risk(tmp_path):
    result, _ = _source(tmp_path, "check_source; echo passed", ALLOW_NONROOT_SOURCE="1")
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout
    assert "Going ahead because ALLOW_NONROOT_SOURCE=1" in result.stderr


def _root_owned_tree() -> Path | None:
    """A real system directory that only root can modify, to stand in for a root-owned checkout."""
    for tree in map(Path, ("/bin", "/sbin", "/usr/libexec", "/usr/sbin")):
        if tree.is_symlink() or not tree.is_dir() or _owned_by_others(tree):
            continue
        if not any(_owned_by_others(Path(dirpath) / name) for dirpath, dirs, files in os.walk(tree)
                   for name in dirs + files if not (Path(dirpath) / name).is_symlink()):
            return tree
    return None


def test_a_checkout_only_root_can_modify_passes(tmp_path):
    tree = _root_owned_tree()
    if tree is None:
        pytest.skip("no root-owned tree to stand in for a root-owned checkout")
    result, _ = _source(tmp_path, f"GATEWAY_SRC={tree}; check_source; echo passed")
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout and result.stderr == ""


@pytest.mark.skipif(not shutil.which("plutil"), reason="plutil is macOS-only")
@pytest.mark.parametrize("edit, complaint", [
    (None, None),
    (["-replace", "UserName", "-string", "root"], "UserName is 'root', expected '_webspec'"),
    (["-replace", "EnvironmentVariables.WEBSPEC_CONFIG", "-string", "/Users/agent/config.json"],
     "EnvironmentVariables.WEBSPEC_CONFIG is '/Users/agent/config.json'"),
    # The whole array: on macOS 27, plutil -replace ProgramArguments.2 inserts an item there.
    (["-replace", "ProgramArguments", "-json", json.dumps(["/bin/sh", "-c", CMD.replace(" -I", ""), "webspec-gateway"])],
     "ProgramArguments.2 is"),
    (["-insert", "ProgramArguments", "-string", "extra", "-append"], "ProgramArguments is '5', expected '4'"),
    (["-insert", "EnvironmentVariables.WEBSPEC_GUARD_KEY", "-string", "k"], "sets the variables"),
    (["-insert", "inetdCompatibility", "-json", '{"Wait": false}'], "has the keys"),
    (["-remove", "Sockets"], "has the keys"),
    (["-insert", "Sockets.other", "-json", '{"SockServiceName": "7003"}'], "has the sockets: gateway other"),
    (["-replace", "Sockets.gateway.SockNodeName", "-string", "0.0.0.0"],
     "Sockets.gateway.SockNodeName is '0.0.0.0', expected '127.0.0.1'"),
    (["-replace", "Sockets.gateway.SockServiceName", "-integer", "7003"],
     "Sockets.gateway.SockServiceName is '7003', expected '7002'"),
    (["-remove", "Sockets.gateway.SockFamily"], "Sockets.gateway has the keys"),
    (["-insert", "Sockets.gateway.SockPassive", "-bool", "false"], "Sockets.gateway has the keys"),
    (["-remove", "EnvironmentVariables.WEBSPEC_LAUNCHD_SOCKET"], "sets the variables"),
    (["-replace", "EnvironmentVariables.WEBSPEC_LAUNCHD_SOCKET", "-string", "other"],
     "EnvironmentVariables.WEBSPEC_LAUNCHD_SOCKET is 'other', expected 'gateway'"),
    (["-insert", "EnvironmentVariables.WEBSPEC_HOST", "-string", "0.0.0.0"], "sets the variables"),
    # launchd's own limit (5 s on macOS 27) would kill the gateway before it finishes the requests
    # in flight; 0 means no limit at all.
    (["-remove", "ExitTimeOut"], "has the keys"),
    (["-replace", "ExitTimeOut", "-integer", "0"], "ExitTimeOut is '0', expected '40'"),
    # Raw, the string "40" prints as the integer 40 does, and "true" as <true/>; launchd.plist(5)
    # defines ExitTimeOut as an integer and RunAtLoad as a boolean.
    (["-replace", "ExitTimeOut", "-string", "40"], "ExitTimeOut is of type 'string', expected 'integer'"),
    (["-replace", "RunAtLoad", "-string", "true"], "RunAtLoad is of type 'string', expected 'bool'"),
], ids=["as-shipped", "user", "config-path", "no-isolation", "extra-argument", "secret", "extra-key", "no-socket",
        "extra-socket", "socket-on-every-address", "socket-port", "socket-family-unset", "socket-extra-key",
        "no-activation", "other-socket-name", "host-back", "default-exit-timeout", "no-exit-timeout",
        "exit-timeout-string", "run-at-load-string"])
def test_installer_refuses_a_plist_that_is_not_the_expected_job(tmp_path, edit, complaint):
    plist = tmp_path / "daemon.plist"
    shutil.copy(DAEMON_PLIST, plist)
    if edit:
        subprocess.run(["plutil", *edit, str(plist)], check=True)
    result, _ = _source(tmp_path, f"PLIST_SRC={plist}; check_plist; echo passed")
    if complaint is None:
        assert result.returncode == 0 and "passed" in result.stdout, result.stderr
    else:
        assert result.returncode == 1 and "passed" not in result.stdout
        assert complaint in result.stderr


# plutil's names for the types plistlib reads.
PLUTIL_TYPES = {bool: "bool", int: "integer", float: "float", str: "string", list: "array", dict: "dictionary"}


def _plist_values(value, path=""):
    """(key path, value) for every value below a dictionary: an array, then each of its items."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _plist_values(item, f"{path}.{key}" if path else key)
        return
    yield path, value
    if isinstance(value, list):
        for i, item in enumerate(value):
            yield from _plist_values(item, f"{path}.{i}")


def test_check_plist_holds_every_value_of_the_shipped_plist_to_its_type(tmp_path):
    # check_plist needs plutil, so compare its table with the plist here too, on any system: every
    # value, by type as well (raw, plutil prints the string "40" as it prints the integer 40), and
    # by its raw value (an array's is its length).
    result, _ = _source(tmp_path, "plist_expected")
    assert result.returncode == 0, result.stderr
    expected = {}
    for line in result.stdout.splitlines():
        path, kind, value = line.split("|", 2)
        assert path not in expected, f"{path} is checked twice"
        expected[path] = (kind, value)
    shipped = {}
    for path, value in _plist_values(_daemon()):
        if isinstance(value, bool):
            raw = "true" if value else "false"
        elif isinstance(value, list):
            raw = str(len(value))
        else:
            raw = str(value)
        shipped[path] = (PLUTIL_TYPES[type(value)], raw)
    assert expected == shipped
    assert expected["ExitTimeOut"] == ("integer", "40") and expected["RunAtLoad"] == ("bool", "true")


def _select_python(tmp_path, **env):
    """Source install.sh and run select_python as a real run would (DRY_RUN=0)."""
    marker = tmp_path / "executed"
    fake = tmp_path / "python3.12"  # owned by the test's user, so a non-root user can modify it
    fake.write_text(f'#!/bin/sh\necho ran >> "{marker}"\nexit 0\n')
    fake.chmod(0o755)
    result, _ = _source(tmp_path, 'select_python; printf "%s|%s\\n" "$PY_REAL" "$PY_UNSAFE"', PYTHON=str(fake), **env)
    return result, fake, marker


@non_root
def test_real_run_refuses_a_python_others_can_modify_without_running_it(tmp_path):
    result, fake, marker = _select_python(tmp_path)
    assert result.returncode == 1
    assert "no root-owned Python 3.11+ found" in result.stderr
    assert str(fake) in result.stderr
    assert "ALLOW_NONROOT_PYTHON=1" in result.stderr
    assert not marker.exists(), "a rejected interpreter must never be executed"


@non_root
def test_allow_nonroot_python_accepts_it_and_names_the_risk(tmp_path):
    result, fake, marker = _select_python(tmp_path, ALLOW_NONROOT_PYTHON="1")
    assert result.returncode == 0, result.stderr
    real, unsafe = result.stdout.strip().split("|")
    assert Path(real).name == "python3.12" and Path(real).samefile(fake)
    assert unsafe, "the modifiable path is reported"
    assert marker.exists()


@non_root
def test_a_venv_others_can_modify_is_rebuilt_without_running_anything_in_it(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    marker = tmp_path / "ran"
    _write_tool(venv / "bin", "python", f'#!/bin/sh\ntouch "{marker}"\n')
    (venv / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).parent}\nexecutable = {sys.executable}\n")
    body = f'VENV={venv}; PY_REAL={sys.executable}; if venv_matches; then echo reuse; else echo rebuild; fi'
    result, _ = _source(tmp_path, body)
    assert result.stdout.strip() == "rebuild", result.stderr
    assert f"can be modified by a user other than root: rebuilding {venv}" in result.stderr
    assert not marker.exists()


ENV_VERDICT = {"0": "loads", "1": "refused: does not load", "2": "refused: unsafe PATH"}


# The plist's PATH as the tests of gateway.env give it, without the venv's directory: a test host
# has none, so the check would judge the /opt above it, which GitHub's runners leave world-writable
# (test_the_plists_own_path_directories_are_judged_too covers that).
ENV_TEST_SVC_PATH = SVC_PATH.removeprefix("/opt/webspec/venv/bin:")


def _check_env_file(tmp_path, content: str, prefix: str = "", svc_path: str = ENV_TEST_SVC_PATH, **env):
    """check_env_file on content; returns (result, all output), result.stdout ending in its verdict:
    loads (0), "refused: does not load" (1, fails closed) or "refused: unsafe PATH" (2)."""
    env_file = tmp_path / "gateway.env"
    env_file.write_text(content)
    body = prefix + (f'SVC_PATH={svc_path}; ENV_FILE={env_file}; STATE={tmp_path}; rc=0; check_env_file || rc=$?; '
                     'echo "verdict $rc"')
    result, calls = _source(tmp_path, body, {"sudo": FAKE_SUDO}, **env)
    assert result.returncode == 0, result.stderr
    # It runs the file as the service user, the way the daemon will, never as root.
    assert any(c.startswith("sudo -u _webspec /usr/bin/env -i ") for c in calls)
    verdict = result.stdout.strip().splitlines()[-1].split()[-1]
    result.stdout = result.stdout.rsplit("verdict ", 1)[0] + ENV_VERDICT[verdict] + "\n"
    return result, result.stdout + result.stderr


def test_gateway_env_that_appends_root_owned_directories_loads(tmp_path):
    result, out = _check_env_file(tmp_path, "WEBSPEC_DOMAIN=example.com\nTICKETS_TOKEN='s3cr3t value'\n"
                                            'PATH="$PATH:/usr/bin"\n')
    assert result.stdout.strip().endswith("loads"), out
    assert "WARNING" not in out
    assert "s3cr3t" not in out


@non_root
def test_gateway_env_that_puts_a_user_writable_directory_on_path_is_not_loaded(tmp_path):
    user_bin = tmp_path / "bin"
    user_bin.mkdir()
    content = f'PATH="$PATH:{user_bin}"\n'
    result, out = _check_env_file(tmp_path, content)
    # Unsafe, not merely broken: a start would run what that user put there (stop_daemon).
    assert result.stdout.strip().endswith("refused: unsafe PATH")
    assert f"{user_bin} ({user_bin} is" in out
    result, out = _check_env_file(tmp_path, content, ALLOW_NONROOT_PATH="1")
    assert result.stdout.strip().endswith("loads")
    assert "Loading anyway because ALLOW_NONROOT_PATH=1" in out


@non_root
def test_a_path_entry_that_is_a_link_another_user_can_repoint_is_not_loaded(tmp_path):
    # The link leads to a root-owned directory, but its owner can point it anywhere.
    link = tmp_path / "bin"
    link.symlink_to(_root_owned_tree() or "/usr/bin")
    result, out = _check_env_file(tmp_path, f'PATH="$PATH:{link}"\n')
    assert result.stdout.strip().endswith("refused: unsafe PATH")
    assert f"{link} ({link} is" in out


def test_root_owned_links_on_path_are_judged_by_where_they_lead(tmp_path):
    # Merged-/usr systems make /bin and /sbin links (mode 0777 on Linux): not a reason to refuse.
    tree = _root_owned_tree()
    if tree is None:
        pytest.skip("no root-owned tree here")
    result, out = _source(tmp_path, f'for p in /bin /sbin {tree}; do if hit=$(unsafe_chain "$p"); then '
                                    f'echo "unsafe $p: $hit"; fi; done; if hit=$(unsafe_entry {tree}); then '
                                    f'echo "unsafe entry: $hit"; fi; echo done')
    assert result.stdout.strip() == "done", result.stdout


# The tests of how links are followed must not depend on who runs them (as root, every file the
# test makes is root-owned; as anyone else, every link it makes is unsafe by its owner alone). So
# they replace unsafe_chain, which the tests above cover, with one that flags exactly the paths
# under the directories given: everything else counts as root's. What remains is the question
# under test: which paths unsafe_entry and unsafe_links hand to unsafe_chain.
def _flagging(*dirs: Path) -> str:
    """Shell that redefines unsafe_chain to flag only paths in (or at) dirs, naming the dir. It
    compares physical paths, as the real one does: a relative link makes a hop like dir/../x."""
    cases = "".join(f'{d}|{d}/*) printf "%s\\n" "{d}"; return 0 ;; ' for d in dirs)
    return ('unsafe_chain() { local p; p=$(CDPATH= cd -P -- "${1%/*}" 2>/dev/null && pwd)/${1##*/} || p=$1; '
            f'case $p in {cases}esac; return 1; }}; ')


def _tree(tmp_path: Path, *dirs: str) -> list[Path]:
    made = []
    for name in dirs:
        (tmp_path / name).mkdir()
        made.append(tmp_path / name)
    return made


def test_entries_of_an_added_path_directory_are_checked_through_their_links(tmp_path):
    # A link whose target sits in a directory another user can write: that user can rename the
    # target and put their own command in its place (finding: only the target file was judged).
    # Without the link followed, nothing here is unsafe, so this fails if unsafe_entry stops
    # following links (finding: the old test passed with find's -L removed).
    pathdir, tools = _tree(tmp_path, "pathdir", "tools")
    (tools / "node").write_text("#!/bin/sh\n")
    (pathdir / "node").symlink_to(tools / "node")
    result, _ = _source(tmp_path, _flagging(tools) + f'unsafe_entry {pathdir} || echo none')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith(f"{pathdir}/node leads through {tools}, which is ")


def test_a_link_to_a_safe_place_passes(tmp_path):
    pathdir, tools, elsewhere = _tree(tmp_path, "pathdir", "tools", "elsewhere")
    (tools / "node").write_text("#!/bin/sh\n")
    (pathdir / "node").symlink_to(tools / "node")
    result, _ = _source(tmp_path, _flagging(elsewhere) + f'unsafe_entry {pathdir} || echo none')
    assert result.stdout.strip() == "none", result.stdout + result.stderr


def test_an_entry_that_is_a_link_to_nothing_is_unsafe(tmp_path):
    # Whoever can later create the target decides what the command is (finding: find -L judged a
    # dangling link by the link itself, which `sudo ln -s` makes root-owned).
    pathdir, tools = _tree(tmp_path, "pathdir", "tools")
    (pathdir / "op").symlink_to(tools / "op")  # not there (yet)
    result, _ = _source(tmp_path, _flagging() + f'unsafe_entry {pathdir} || echo none')
    assert result.stdout.strip() == f"{pathdir}/op is a symbolic link to nothing", result.stdout + result.stderr
    # One hop further: the link it leads to is the one that leads nowhere.
    (tools / "op").symlink_to(tools / "op-1.2")
    result, _ = _source(tmp_path, _flagging() + f'unsafe_entry {pathdir} || echo none')
    assert result.stdout.strip() == f"{pathdir}/op leads through {tools}/op, which is a symbolic link to nothing"
    (tools / "op-1.2").write_text("#!/bin/sh\n")
    result, _ = _source(tmp_path, _flagging() + f'unsafe_entry {pathdir} || echo none')
    assert result.stdout.strip() == "none"


def test_every_hop_of_a_chain_of_links_is_checked(tmp_path):
    # pathdir/op -> middle/op -> tools/op: the middle link's directory can be written, so its
    # link can be repointed, though the first link and the final file are both safe.
    pathdir, middle, tools = _tree(tmp_path, "pathdir", "middle", "tools")
    (tools / "op").write_text("#!/bin/sh\n")
    (middle / "op").symlink_to(tools / "op")
    (pathdir / "op").symlink_to("../middle/op")  # relative: resolved from the link's directory
    result, _ = _source(tmp_path, _flagging(middle) + f'unsafe_entry {pathdir} || echo none')
    assert result.stdout.strip().startswith(f"{pathdir}/op leads through {middle}, which is "), result.stdout
    result, _ = _source(tmp_path, _flagging(tools) + f'unsafe_entry {pathdir} || echo none')
    assert result.stdout.strip().startswith(f"{pathdir}/op leads through {tools}, which is "), result.stdout


@pytest.mark.parametrize("suffix", ["", "/", "/.", "//", "/./"], ids=["plain", "slash", "slash-dot", "slashes",
                                                                      "slash-dot-slash"])
def test_every_hop_of_a_path_entry_is_checked_however_it_is_written(tmp_path, suffix):
    # opt/bin -> middle/bin -> real: the middle link's directory can be written. Written with a
    # trailing slash, [ -L ] follows the first link instead of seeing it, and the walk used to stop
    # there (review); PATH means the same directory either way.
    opt, middle, real = _tree(tmp_path, "opt", "middle", "real")
    (middle / "bin").symlink_to(real)
    (opt / "bin").symlink_to(middle / "bin")
    entry = f"{opt / 'bin'}{suffix}"
    result, _ = _source(tmp_path, _flagging(middle) + f'unsafe_links {shlex.quote(entry)} || echo safe')
    assert result.stdout.strip() == str(middle), result.stdout + result.stderr
    # Where trailing slashes go, and where they do not.
    result, _ = _source(tmp_path, 'for p in /a/b/ /a/b/. /a/b//./ / // /. a/ ""; do printf "[%s]" "$(trim_path "$p")"; done')
    assert result.stdout == "[/a/b][/a/b][/a/b][/][/][/.][a][]"


def test_gateway_env_with_a_slashed_path_entry_is_checked_through_its_links(tmp_path):
    opt, middle, real = _tree(tmp_path, "opt", "middle", "real")
    (middle / "bin").symlink_to(real)
    (opt / "bin").symlink_to(middle / "bin")
    result, out = _check_env_file(tmp_path, f'PATH="$PATH:{opt / "bin"}/"\n', prefix=_flagging(middle))
    assert result.stdout.strip().endswith("refused: unsafe PATH"), out
    assert f"{opt / 'bin'} ({middle} is " in out


def test_the_plists_own_path_directories_are_judged_too(tmp_path):
    # Where others can write /opt (GitHub's runners leave it so), any user can rename /opt/webspec
    # and put a venv of their own in its place.
    result, out = _check_env_file(tmp_path, "WEBSPEC_DOMAIN=example.com\n", prefix=_flagging(Path("/opt")),
                                  svc_path=SVC_PATH)
    assert result.stdout.strip().endswith("refused: unsafe PATH"), out
    assert "/opt/webspec/venv/bin (/opt is " in out


def test_links_in_a_loop_are_unsafe_and_end(tmp_path):
    pathdir, = _tree(tmp_path, "pathdir")
    (pathdir / "a").symlink_to(pathdir / "b")
    (pathdir / "b").symlink_to(pathdir / "a")
    result, _ = _source(tmp_path, _flagging() + f'unsafe_entry {pathdir} || echo none')
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(rf"{re.escape(str(pathdir))}/[ab] is a symbolic link to nothing", result.stdout.strip()), \
        result.stdout


def test_a_path_directory_that_is_a_link_is_searched_where_it_leads(tmp_path):
    # The entries of the directory a PATH link leads to are what the gateway's servers run.
    # The world-writable command is unsafe for any user running this test.
    real, = _tree(tmp_path, "real")
    (real / "node").write_text("#!/bin/sh\n")
    (real / "node").chmod(0o777)
    (tmp_path / "pathlink").symlink_to(real)
    result, _ = _source(tmp_path, f'unsafe_entry {tmp_path / "pathlink"} || echo none')
    assert result.stdout.strip().startswith(f"{tmp_path / 'pathlink'}/node is "), result.stdout


def test_gateway_env_whose_path_directory_links_into_an_unsafe_place_is_not_loaded(tmp_path):
    # check_env_file, end to end: /opt/webspec/bin-style directory, root-owned, holding a
    # root-owned link to a command in a directory that another user can write (Intel Homebrew's
    # /usr/local/bin, for one).
    pathdir, tools = _tree(tmp_path, "pathdir", "tools")
    (tools / "op").write_text("#!/bin/sh\n")
    (pathdir / "op").symlink_to(tools / "op")
    env_file = tmp_path / "gateway.env"
    env_file.write_text(f'PATH="$PATH:{pathdir}"\n')
    body = (_flagging(tools) + f'ENV_FILE={env_file}; STATE={tmp_path}; '
            'if check_env_file; then echo loads; else echo refused; fi')
    result, _ = _source(tmp_path, body, {"sudo": FAKE_SUDO})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("refused"), result.stdout + result.stderr
    assert f"{pathdir} ({pathdir}/op leads through {tools}, which is " in result.stderr


def test_a_path_entry_that_is_a_link_to_nothing_is_not_loaded(tmp_path):
    env_file = tmp_path / "gateway.env"
    (tmp_path / "bin").symlink_to(tmp_path / "not-yet")
    env_file.write_text(f'PATH="$PATH:{tmp_path / "bin"}"\n')
    body = (_flagging() + f'ENV_FILE={env_file}; STATE={tmp_path}; '
            'if check_env_file; then echo loads; else echo refused; fi')
    result, _ = _source(tmp_path, body, {"sudo": FAKE_SUDO})
    assert result.stdout.strip().endswith("refused"), result.stdout + result.stderr
    assert f"{tmp_path / 'bin'} ({tmp_path / 'bin'} is a symbolic link to nothing)" in result.stderr


@pytest.mark.parametrize("path", ["relbin", "./x", ""])
def test_a_relative_path_is_never_safe(tmp_path, path):
    result, _ = _source(tmp_path, f'unsafe_chain {shlex.quote(path)} && echo unsafe || echo safe')
    assert result.stdout.strip().endswith("unsafe")


def test_gateway_env_with_relative_or_empty_path_entries_is_not_loaded(tmp_path):
    result, out = _check_env_file(tmp_path, 'PATH="relbin:$PATH:"\n')
    assert result.stdout.strip().endswith("refused: unsafe PATH")
    assert "relbin (a relative path)" in out and "an empty entry (the working directory)" in out
    assert "does not keep the plist's PATH first" in out


@pytest.mark.parametrize("content", ["TICKETS_TOKEN=abc s3cr3tpart\nWEBSPEC_DOMAIN=x\n",
                                     "TICKETS_TOKEN='s3cr3tpart\n"], ids=["failing-line", "syntax-error"])
def test_gateway_env_that_fails_to_load_is_not_loaded_and_its_text_is_not_echoed(tmp_path, content):
    # Broken, not unsafe: the daemon would exit at every start (hold_back keeps a loaded one).
    result, out = _check_env_file(tmp_path, content)
    assert result.stdout.strip().endswith("refused: does not load")
    assert "does not load in /bin/sh" in out and "line " in out
    assert "s3cr3tpart" not in out


def test_gateway_env_that_ends_the_shell_does_not_load(tmp_path):
    # Its PATH is never printed, which must not read as an empty, unsafe PATH (stop_daemon).
    result, out = _check_env_file(tmp_path, "WEBSPEC_DOMAIN=example.com\nexit 0\n")
    assert result.stdout.strip().endswith("refused: does not load"), out
    assert "ends the shell that loads it, so the daemon would never start the gateway" in out


SHELLS = sorted({os.path.realpath(s) for s in ("/bin/sh", shutil.which("dash"), shutil.which("bash")) if s})


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("content, lines", [("A=1\nTOKEN=abc s3cr3tpart\n", {2}), ("A=1\nB=2\nTOKEN='s3cr3tpart\n", {3, 4})],
                         ids=["failing-line", "syntax-error"])
def test_load_errors_are_reported_by_line_number_only(tmp_path, shell, content, lines):
    # /bin/sh is bash on macOS unless /var/select/sh points it at dash; their messages differ (an
    # unterminated quote is reported where it opens by bash, at the end of the file by dash).
    env_file = tmp_path / "gateway.env"
    env_file.write_text(content)
    err = subprocess.run([shell, "-c", 'set -ae; . "$1"', "sh", str(env_file)], capture_output=True,
                         text=True, timeout=30).stderr
    result, _ = _source(tmp_path, 'error_lines "$ERR"; echo', ERR=err)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() in {f"(line {n})" for n in lines}, err
    assert "s3cr3tpart" not in result.stdout


def test_gateway_env_overrides_are_named_but_their_values_are_not_printed(tmp_path):
    result, out = _check_env_file(tmp_path, "WEBSPEC_HOST=0.0.0.0\nexport WEBSPEC_GUARD_KEY=s3cr3tkey\n"
                                            "WEBSPEC_ACCESS_LOG=1\nWEBSPEC_INTERNAL_PORT=7012\n")
    assert result.stdout.strip().endswith("loads")
    assert "sets WEBSPEC_HOST, which has no effect: the gateway listens only on the socket" in out
    assert "overrides WEBSPEC_INTERNAL_PORT, which the plist sets" in out
    assert "sets WEBSPEC_GUARD_KEY, which the gateway uses instead of /etc/webspec/guard.key" in out
    assert "sets WEBSPEC_ACCESS_LOG" in out
    assert "0.0.0.0" not in out and "s3cr3tkey" not in out and "7012" not in out


def test_gateway_env_that_restates_the_plist_or_clears_a_default_is_not_named(tmp_path):
    # Judged by the environment gateway.env leaves, not by the lines it holds.
    result, out = _check_env_file(tmp_path, "WEBSPEC_PORT=7001\nexport WEBSPEC_LAUNCHD_SOCKET=gateway\n"
                                            "WEBSPEC_CORS_ORIGINS=\nWEBSPEC_ACCESS_LOG=\n")
    assert result.stdout.strip().endswith("loads")
    assert "WARNING" not in out, out


@pytest.mark.parametrize("content", [
    "export WEBSPEC_CORS_ORIGINS='https://evil.example'\n",
    "WEBSPEC_DOMAIN=example.com WEBSPEC_CORS_ORIGINS=https://evil.example\n",
    "export WEBSPEC_DOMAIN=example.com; WEBSPEC_CORS_ORIGINS=https://evil.example\n",
    ': "${WEBSPEC_CORS_ORIGINS:=https://evil.example}"\n',
], ids=["export", "two-on-a-line", "after-a-semicolon", "default-expansion"])
def test_gateway_env_that_opens_cors_is_named(tmp_path, content):
    # DP-8 (finding: the one setting that lets other origins read answers went unmentioned;
    # review: only an assignment at the start of a line was seen).
    result, out = _check_env_file(tmp_path, content)
    assert result.stdout.strip().endswith("loads")
    assert "sets WEBSPEC_CORS_ORIGINS: pages from those origins can read the gateway's answers" in out
    assert "and send it unsafe methods (DP-8)" in out
    assert "evil.example" not in out


def _warnings(text: str) -> str:
    """The WARNING lines of text, joined into one."""
    return " ".join(ln[len("WARNING: "):] for ln in text.splitlines() if ln.startswith("WARNING: "))


@pytest.mark.parametrize("content", ["WEBSPEC_LAUNCHD_SOCKET=\n", "unset WEBSPEC_LAUNCHD_SOCKET\n",
                                     "export WEBSPEC_DOMAIN=x WEBSPEC_LAUNCHD_SOCKET=other\n"],
                         ids=["emptied", "unset", "renamed"])
def test_gateway_env_that_turns_off_socket_activation_is_named(tmp_path, content):
    # DP-9: without the socket launchd holds, the port is free whenever the gateway is down.
    result, out = _check_env_file(tmp_path, content)
    assert "overrides WEBSPEC_LAUNCHD_SOCKET, which the plist sets. The gateway must take the socket " \
           "'gateway' that launchd holds on 127.0.0.1:7002, or another process can take the port (DP-9)." \
           in _warnings(out)


# The probe runs under `env -i`, so this fake gets its log path and its answer baked in.
FAKE_VENV_PYTHON = """#!/bin/sh
printf 'python %s | %s\\n' "$1 $2" "$(/usr/bin/env | /usr/bin/sort | /usr/bin/tr '\\n' ' ')" >> {log}
if [ -n {error} ]; then echo {error} >&2; exit 1; fi
"""


@pytest.mark.parametrize("error", ["", "WEBSPEC_GUARD_KEY is not set. Source it from your password manager"])
def test_daemon_is_loaded_only_if_the_installed_gateway_can_load_the_key_file(tmp_path, error):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    _write_tool(venv / "bin", "python", FAKE_VENV_PYTHON.format(log=shlex.quote(str(tmp_path / "calls.log")),
                                                                error=shlex.quote(error)))
    body = f'VENV={venv}; STATE=/var/lib/webspec; if check_guard_key; then echo loads; else echo refused; fi'
    result, calls = _source(tmp_path, body, {"sudo": FAKE_SUDO})
    assert result.returncode == 0, result.stderr
    assert "sudo -u _webspec /usr/bin/env -i HOME=/var/lib/webspec PATH=/usr/bin:/bin " \
           f"WEBSPEC_GUARD_KEY_FILE=/etc/webspec/guard.key {venv}/bin/python -I -c" in "\n".join(calls)
    # Isolated mode, and an environment holding only what the probe needs (no key, no secret).
    probe = next(c for c in calls if c.startswith("python "))
    assert probe.startswith("python -I -c | ")
    seen = dict(item.split("=", 1) for item in probe.split(" | ", 1)[1].split())
    assert set(seen) <= {"HOME", "PATH", "WEBSPEC_GUARD_KEY_FILE", "PWD", "SHLVL", "_"}
    assert (seen["HOME"], seen["PATH"], seen["WEBSPEC_GUARD_KEY_FILE"]) == (
        "/var/lib/webspec", "/usr/bin:/bin", "/etc/webspec/guard.key")
    if error:
        assert result.stdout.strip().endswith("refused")
        assert error in result.stderr
        assert "cannot load the guard key from /etc/webspec/guard.key" in result.stderr
    else:
        assert result.stdout.strip() == "loads"


PORT_TOOLS = {"lsof": FAKE_LSOF, "ps": FAKE_PS, "launchctl": FAKE_LAUNCHCTL_LIST, "curl": FAKE_CURL}


def test_daemon_is_not_loaded_while_another_process_holds_the_gateway_address(tmp_path):
    result, _ = _source(tmp_path, "if check_gateway_port; then echo loads; else echo refused; fi", PORT_TOOLS,
                        FAKE_LSOF_7002="4242", FAKE_PS_USER="agent")
    assert result.stdout.strip().endswith("refused")
    assert "127.0.0.1:7002 is held by 4242 (agent, python3.12) on 127.0.0.1:7002. launchd cannot bind it" \
           in _warnings(result.stderr)
    # PID 1 too: after the daemon's bootout, launchd holds that address only for another job.
    result, _ = _source(tmp_path, "if check_gateway_port; then echo loads; else echo refused; fi", PORT_TOOLS,
                        FAKE_LSOF_7002="1")
    assert result.stdout.strip().endswith("refused")


@pytest.mark.parametrize("listeners", ["4242@*:7002", "4242@[::1]:7002 4243@192.168.1.5:7002"],
                         ids=["wildcard", "other-addresses"])
def test_a_listener_on_another_address_does_not_hold_back_the_bootstrap(tmp_path, listeners):
    # Review: refusing left a pre-placed *:7002 listener all of Caddy's traffic. launchd's
    # 127.0.0.1:7002, the more specific socket, takes 127.0.0.1's connections from it, and
    # check_health then names it (UNHEALTHY: the run exits 1).
    result, _ = _source(tmp_path, "if check_gateway_port; then echo loads; else echo refused; fi", PORT_TOOLS,
                        FAKE_LSOF_7002=listeners, FAKE_PS_USER="agent")
    assert result.stdout.strip() == "loads", result.stderr


def test_an_unclaimed_caddy_port_is_reported(tmp_path):
    result, _ = _source(tmp_path, "if check_caddy_port; then echo loads; else echo refused; fi", PORT_TOOLS)
    assert result.stdout.strip() == "loads"
    assert "nothing listens on port 7001 yet, so any local user can take it" in result.stderr


@pytest.mark.parametrize("command", ["/Users/devuser/miniconda3/bin/python -m webspec",
                                     "/opt/homebrew/bin/python3.12 -Im webspec",
                                     "python3 -I -m webspec --reload"],
                         ids=["dev-plist", "combined-flags", "extra-args"])
def test_another_gateway_on_the_caddy_port_holds_back_the_daemon(tmp_path, command):
    # DP-7, DP-1 (finding: a dev gateway on 7001 was only mentioned, and the daemon loaded beside
    # it). It holds the port the tunnel delivers to, as a login user.
    result, _ = _source(tmp_path, "if check_caddy_port; then echo loads; else echo refused; fi", PORT_TOOLS,
                        FAKE_LSOF_7001="4242", FAKE_PS_USER="devuser", FAKE_PS_UID="501", FAKE_PS_COMMAND=command)
    assert result.stdout.strip().endswith("refused")
    assert "port 7001 is held by a WebSpec gateway: 4242 (devuser, python3.12) runs -m webspec" in result.stderr


@pytest.mark.parametrize("uid, warned", [("501", True), ("250", False)], ids=["login-user", "service-user"])
def test_the_caddy_ports_holder_is_named(tmp_path, uid, warned):
    result, _ = _source(tmp_path, "if check_caddy_port; then echo loads; else echo refused; fi", PORT_TOOLS,
                        FAKE_LSOF_7001="4242", FAKE_PS_USER="caddy", FAKE_PS_UID=uid,
                        FAKE_PS_COMMAND="/usr/local/bin/caddy run --config /etc/caddy/Caddyfile")
    assert result.stdout.strip().endswith("loads")
    assert "port 7001: 4242 (caddy, python3.12)" in result.stdout
    assert ("a login user's process. Run Caddy as its own" in result.stderr) is warned


@pytest.mark.parametrize("listeners, owner, healthy, message", [
    ("999", "_webspec", True, "the gateway (PID 999, user _webspec) answers on 127.0.0.1:7002 (GET / for Host localhost: HTTP 200)"),
    # launchd keeps its own descriptor of the socket it hands the gateway.
    ("1 999", "_webspec", True, "the gateway (PID 999, user _webspec) answers on 127.0.0.1:7002"),
    ("4242", "agent", False, "port 7002 is held by 4242 (agent, python3.12) on 127.0.0.1:7002, not only by launchd and "
                             "com.webspec.gateway.daemon (PID 999) on 127.0.0.1:7002"),
    ("1 999 4242", "agent", False, "port 7002 is held by 4242 (agent, python3.12) on 127.0.0.1:7002, not only by launchd"),
    # Review: a listener on another address of the port takes Caddy's traffic the moment launchd
    # lets go of 127.0.0.1:7002, and launchd binds other jobs' sockets too (a login user's agent).
    ("1 999 4242@*:7002", "agent", False, "port 7002 is held by 4242 (agent, python3.12) on *:7002"),
    ("1 999 1@*:7002", "root", False, "port 7002 is held by 1 (root, python3.12) on *:7002"),
    ("1", "_webspec", False, "the gateway has not taken the socket that launchd holds on 127.0.0.1:7002"),
    ("999", "agent", False, "com.webspec.gateway.daemon (PID 999) runs as 'agent', not _webspec"),
], ids=["the-daemon", "launchd-and-the-daemon", "another-process", "another-process-too", "wildcard-listener",
        "another-launchd-socket", "not-taken", "wrong-user"])
def test_health_check_accepts_only_the_daemon_itself_as_the_service_user(tmp_path, listeners, owner, healthy, message):
    # An HTTP answer alone proves nothing: the port is free between a bootout and a bootstrap.
    result, _ = _source(tmp_path, 'HEALTH_TRIES=2; check_health; echo "unhealthy=$UNHEALTHY"', PORT_TOOLS,
                        FAKE_LSOF_7002=listeners, FAKE_DAEMON_PID="999", FAKE_PS_USER=owner)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(f"unhealthy={0 if healthy else 1}")
    assert message in result.stdout + _warnings(result.stderr)


# Fakes whose answers change from call to call: the Nth call gets the Nth word of FAKE_SEQ_<tool>
# (the last one from then on).
FAKE_SEQ = """#!/bin/sh
for a; do case $a in -F) fmt=F ;; -iTCP:*) port=${{a#-iTCP:}} ;; esac; done
n=$(($(cat "$FAKE_DIR/{name}.n" 2>/dev/null || echo 0) + 1)); echo "$n" > "$FAKE_DIR/{name}.n"
set -- $FAKE_SEQ_{name}; while [ "$n" -gt 1 ] && [ "$#" -gt 1 ]; do shift; n=$((n - 1)); done
{body}
"""
# Each word: the listeners as FAKE_LSOF takes them, separated by commas, or - for none.
FAKE_LSOF_SEQ = FAKE_SEQ.format(name="lsof", body="""[ "$1" = - ] && exit 1
for i in $(echo "$1" | tr , ' '); do
  pid=${i%%@*}
  case $i in *@*) addr=${i#*@} ;; *) addr=127.0.0.1:$port ;; esac
  if [ "${fmt:-t}" = t ]; then echo "$pid"; else printf 'p%s\\nf3\\nn%s\\n' "$pid" "$addr"; fi
done""")
FAKE_LAUNCHCTL_LIST_SEQ = FAKE_SEQ.format(
    name="launchctl", body="printf 'PID\\tStatus\\tLabel\\n%s\\t0\\tcom.webspec.gateway.daemon\\n' \"$1\"")


def test_health_check_waits_out_the_instance_it_replaced(tmp_path):
    # Across kickstart -k: the old gateway still listed and listening, then neither, then the new
    # one listed and holding the socket. Only the new one counts, and it is worth waiting for.
    tools = {"lsof": FAKE_LSOF_SEQ, "launchctl": FAKE_LAUNCHCTL_LIST_SEQ, "ps": FAKE_PS, "curl": FAKE_CURL}
    result, _ = _source(tmp_path, 'HEALTH_TRIES=6; check_health 999; echo "unhealthy=$UNHEALTHY"', tools,
                        FAKE_DIR=str(tmp_path), FAKE_SEQ_lsof="1,999 1 1,1000", FAKE_SEQ_launchctl="999 1000",
                        FAKE_PS_USER="_webspec")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("unhealthy=0"), result.stdout + result.stderr
    assert "the gateway (PID 1000, user _webspec) answers on 127.0.0.1:7002" in result.stdout


@pytest.mark.parametrize("replaced, launchctl, lsof, healthy", [
    ("999", "999 999 999 999 1000", "1,999 1,999 1,999 1,999 1,1000", True),
    ("", "- - - - 1000", "1 1 1 1 1,1000", False),
], ids=["kickstart", "first-start"])
def test_health_check_gives_the_instance_it_replaced_the_exit_timeout_to_go(tmp_path, replaced, launchctl, lsof,
                                                                             healthy):
    # kickstart -k: the gateway it replaces may finish requests in flight for up to ExitTimeOut
    # seconds before the new one starts, which is no failure. A first start replaces nothing, so
    # HEALTH_TRIES alone applies. Here the new gateway takes the socket at the fifth look.
    tools = {"lsof": FAKE_LSOF_SEQ, "launchctl": FAKE_LAUNCHCTL_LIST_SEQ, "ps": FAKE_PS, "curl": FAKE_CURL}
    result, _ = _source(tmp_path, f'HEALTH_TRIES=2; EXIT_TIMEOUT=2; check_health {replaced}; echo "unhealthy=$UNHEALTHY"',
                        tools, FAKE_DIR=str(tmp_path), FAKE_SEQ_launchctl=launchctl, FAKE_SEQ_lsof=lsof,
                        FAKE_PS_USER="_webspec")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(f"unhealthy={0 if healthy else 1}"), result.stdout + result.stderr
    if healthy:
        assert "the gateway (PID 1000, user _webspec) answers on 127.0.0.1:7002" in result.stdout
    else:
        assert "has not taken the socket that launchd holds on 127.0.0.1:7002 after 1 s" in _warnings(result.stderr)


def test_health_check_after_a_restart_waits_for_the_new_gateway(tmp_path):
    # After kickstart -k, the instance that answers must be a new one, not the one replaced.
    result, _ = _source(tmp_path, 'HEALTH_TRIES=2; EXIT_TIMEOUT=0; check_health 999; echo "unhealthy=$UNHEALTHY"',
                        PORT_TOOLS, FAKE_LSOF_7002="1 999", FAKE_DAEMON_PID="999", FAKE_PS_USER="_webspec")
    assert result.stdout.strip().endswith("unhealthy=1")
    assert "has not taken the socket" in result.stderr
    result, _ = _source(tmp_path, 'HEALTH_TRIES=2; check_health 998; echo "unhealthy=$UNHEALTHY"', PORT_TOOLS,
                        FAKE_LSOF_7002="1 999", FAKE_DAEMON_PID="999", FAKE_PS_USER="_webspec")
    assert result.stdout.strip().endswith("unhealthy=0"), result.stderr


SECRET_ENV = "# site settings\nWEBSPEC_DOMAIN=old.example\nTICKETS_TOKEN='s3cr3t'\nexport WEBSPEC_DOMAIN=older.example\n"


@pytest.mark.parametrize("domain, expected", [
    ("new.example", "# site settings\nWEBSPEC_DOMAIN=new.example\nTICKETS_TOKEN='s3cr3t'\n"),
    ("", "# site settings\nWEBSPEC_DOMAIN=\nTICKETS_TOKEN='s3cr3t'\n"),
], ids=["set", "clear"])
def test_webspec_domain_is_updated_in_gateway_env_and_the_rest_is_kept(tmp_path, domain, expected):
    env_file = tmp_path / "gateway.env"
    env_file.write_text(SECRET_ENV)
    body = f'ENV_FILE={env_file}; SVC_GROUP=staff; DOMAIN={shlex.quote(domain)}; set_env_domain'
    result, calls = _source(tmp_path, body, {"install": FAKE_INSTALL})
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == expected
    assert "s3cr3t" not in result.stdout + result.stderr
    assert any(c.startswith("install -m 0640 -o root -g staff ") for c in calls)
    # Again with the same value: nothing to install.
    result, calls = _source(tmp_path, body, {"install": FAKE_INSTALL})
    assert f"WEBSPEC_DOMAIN in {env_file} is already '{domain}'" in result.stdout
    assert sum(c.startswith("install ") for c in calls) == 1


def test_webspec_domain_is_appended_when_gateway_env_has_none(tmp_path):
    env_file = tmp_path / "gateway.env"
    env_file.write_text("TICKETS_TOKEN='s3cr3t'\n")
    result, _ = _source(tmp_path, f"ENV_FILE={env_file}; SVC_GROUP=staff; DOMAIN=example.com; set_env_domain",
                        {"install": FAKE_INSTALL})
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "TICKETS_TOKEN='s3cr3t'\nWEBSPEC_DOMAIN=example.com\n"


def test_an_edited_installed_plist_is_shown_before_it_is_replaced(tmp_path):
    installed = tmp_path / "installed.plist"
    installed.write_text(DAEMON_PLIST.read_text().replace("<string>7002</string>", "<string>7012</string>"))
    result, _ = _source(tmp_path, f"PLIST_DST={installed}; install_plist", {"install": FAKE_INSTALL, "plutil": FAKE_PLUTIL})
    assert result.returncode == 0, result.stderr
    assert f"{installed} differs from {DAEMON_PLIST} and is replaced" in result.stderr
    assert "-        <string>7012</string>" in result.stderr
    assert installed.read_bytes() == DAEMON_PLIST.read_bytes()


@pytest.mark.parametrize("before, same", [("same", 1), ("edited", 0), (None, 0)], ids=["same", "edited", "none"])
def test_the_installer_knows_whether_the_installed_plist_was_already_this_one(tmp_path, before, same):
    # Only then can a loaded job be restarted in place, keeping its socket (load_daemon).
    installed = tmp_path / "installed.plist"
    if before == "same":
        shutil.copy(DAEMON_PLIST, installed)
    elif before == "edited":
        installed.write_text(DAEMON_PLIST.read_text().replace("Standard", "Background"))
    result, _ = _source(tmp_path, f'PLIST_DST={installed}; install_plist; echo "same=$PLIST_SAME"',
                        {"install": FAKE_INSTALL, "plutil": FAKE_PLUTIL})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(f"same={same}")
    assert ("differs from" in result.stderr) is (before == "edited")


# ── The installer: loading, not loading ──


FAKE_LAUNCHCTL_JOB = r"""#!/bin/sh
# The system job is loaded while the file $FAKE_JOB exists, and `launchctl print` shows its
# content. bootstrap loads it from the plist, with the socket, as launchd would.
# FAKE_BOOTOUT_FAILS=1: bootout fails, and the job stays loaded; FAKE_BOOTSTRAP_FAILS=1 likewise.
printf 'launchctl %s\n' "$*" >> "$FAKE_LOG"
case "$*" in
  "print system/com.webspec.gateway.daemon")
    [ -f "$FAKE_JOB" ] || exit 113
    cat "$FAKE_JOB" ;;
  "print "*) exit 113 ;;
  "bootout system/com.webspec.gateway.daemon")
    if [ -n "${FAKE_BOOTOUT_FAILS:-}" ]; then echo "Boot-out failed: 5: Input/output error" >&2; exit 5; fi
    rm -f "$FAKE_JOB" ;;
  "bootstrap system "*)
    if [ -n "${FAKE_BOOTSTRAP_FAILS:-}" ]; then echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi
    printf 'system/com.webspec.gateway.daemon = {\n\tsockets = {\n\t\t"gateway" = {\n\t\t}\n\t}\n}\n' > "$FAKE_JOB" ;;
  "enable system/com.webspec.gateway.daemon" | "disable system/com.webspec.gateway.daemon" | \
  "kickstart -k system/com.webspec.gateway.daemon") ;;
  list) printf 'PID\tStatus\tLabel\n%s\t0\tcom.webspec.gateway.daemon\n' "${FAKE_DAEMON_PID:--}" ;;
  *) echo "fake launchctl refuses: $*" >&2; exit 99 ;;
esac
"""

# A job as launchd shows it once loaded from the plist (with its socket), and as it was loaded
# from the plist before it had one.
JOB_WITH_SOCKET = 'system/com.webspec.gateway.daemon = {\n\tsockets = {\n\t\t"gateway" = {\n\t\t}\n\t}\n}\n'
JOB_WITHOUT_SOCKET = "system/com.webspec.gateway.daemon = {\n\tstate = running\n}\n"

# Every check passes, unless a case redefines one or keeps the real one; check_health only says
# that it was called, and for which replaced instance.
PASSING = {
    "key_state": "key_state() { return 0; }; ",
    "check_env_file": "check_env_file() { return 0; }; ",
    "check_guard_key": "check_guard_key() { return 0; }; ",
    "check_caddy_port": "check_caddy_port() { return 0; }; ",
    "check_gateway_port": "check_gateway_port() { return 0; }; ",
    "check_health": 'check_health() { echo "health ${1:-new}"; }; ',
}

# Why a run does not (re)start the daemon. The first four are its own configuration, with which
# the gateway fails closed; the last two are other processes, which any login user can start.
HOLDING_BACK = {
    "key-empty": "key_state() { return 1; }; ",
    "key-unusable": "key_state() { return 2; }; ",
    "env-file-does-not-load": "check_env_file() { return 1; }; ",
    "key-not-loadable": "check_guard_key() { return 1; }; ",
    "dev-agent": "DEV_AGENT=gui/501/com.webspec.gateway; ",
    "gateway-on-7001": "check_caddy_port() { return 1; }; ",
}
OTHER_PROCESSES = ("dev-agent", "gateway-on-7001")
# gateway.env puts on PATH what another user can modify: a start would run that user's commands.
UNSAFE_PATH = "check_env_file() { return 2; }; "
OUTCOME = ("KEY_STATE", "LOADED", "HELD", "STOPPED", "FAILURE")


def _load(tmp_path, body: str, job: str | None, plist_same: bool = True, real: tuple[str, ...] = (),
          tools: dict[str, str] | None = None, finish: bool = False, **env):
    """Source install.sh and run load_daemon (DRY_RUN=0) with the job loaded as `job` (None: not
    loaded) and the checks in `real` left as they are; nothing listens on 7002 unless FAKE_LSOF_7002
    says so. With finish, the run's last step follows, which exits 1 on a failure. Returns (result,
    the launchctl calls that change something, whether the job is loaded afterwards, OUTCOME)."""
    job_file = tmp_path / "job"
    if job is not None:
        job_file.write_text(job)
    elif job_file.exists():
        job_file.unlink()
    script = ("".join(v for k, v in PASSING.items() if k not in real) + f"PLIST_SAME={int(plist_same)}; " + body +
              'load_daemon; printf "%s\\n" ' + " ".join(f'"{name}=${name}"' for name in OUTCOME) +
              ("; finish; echo finished" if finish else ""))
    result, calls = _source(tmp_path, script, {"launchctl": FAKE_LAUNCHCTL_JOB, "lsof": FAKE_LSOF, "ps": FAKE_PS,
                                               **(tools or {})},
                            FAKE_JOB=str(job_file), FAKE_DAEMON_PID="999", **env)
    mutations = [c[len("launchctl "):] for c in calls
                 if c.startswith("launchctl ") and not c.startswith(("launchctl print", "launchctl list"))]
    outcome = dict(ln.split("=", 1) for ln in result.stdout.splitlines() if ln.split("=", 1)[0] in OUTCOME)
    return result, mutations, job_file.exists(), outcome


@pytest.mark.parametrize("case", sorted(HOLDING_BACK) + ["unsafe-path"])
def test_a_refused_run_disables_a_daemon_that_is_not_loaded(tmp_path, case):
    # F13: launchd loads every enabled plist in /Library/LaunchDaemons at boot, so a run that does
    # not load the daemon disables it; the enable before the next bootstrap undoes that. Nothing
    # held the port before, nothing is stopped, and a first install that waits for its key is no
    # failure.
    body = UNSAFE_PATH if case == "unsafe-path" else HOLDING_BACK[case]
    result, mutations, loaded, outcome = _load(tmp_path, body, None, finish=True)
    assert result.returncode == 0, result.stderr
    assert mutations == ["disable system/com.webspec.gateway.daemon"]
    assert not loaded
    assert "not loading: " in result.stdout
    assert "stays disabled, at boot too, until a run of this installer passes these checks" in result.stdout
    assert (outcome["LOADED"], outcome["HELD"], outcome["STOPPED"], outcome["FAILURE"]) == ("0", "0", "0", "")
    assert result.stdout.strip().endswith("finished")


@pytest.mark.parametrize("case", sorted(HOLDING_BACK))
def test_a_refused_run_leaves_a_loaded_daemon_running_and_holding_its_port(tmp_path, case):
    # DP-9 (review: a refused re-run booted the daemon out, which freed 127.0.0.1:7002 for any
    # local process, and the agent's user can bring about the last two refusals at will). launchd
    # keeps the port while the job stays loaded. With a refused key or gateway.env the gateway
    # fails closed; a development gateway is not stopped by stopping this one.
    result, mutations, loaded, outcome = _load(tmp_path, HOLDING_BACK[case], JOB_WITH_SOCKET, finish=True)
    assert mutations == []
    assert loaded
    assert "not restarting com.webspec.gateway.daemon: " in result.stdout
    assert "launchd keeps holding 127.0.0.1:7002 for it" in result.stdout
    assert ("the gateway fails closed" in result.stdout) is (case not in OTHER_PROCESSES)
    assert (outcome["LOADED"], outcome["HELD"], outcome["STOPPED"]) == ("0", "1", "0")
    assert "health" not in result.stdout
    # Nor does the run say that it succeeded (review: it exited 0).
    assert result.returncode == 1 and "finished" not in result.stdout
    assert "install.sh: com.webspec.gateway.daemon was not restarted: it runs as it did before this run" \
           in result.stderr


def test_a_held_back_job_from_a_plist_without_the_socket_is_described_as_it_is(tmp_path):
    # Its gateway holds the port itself; launchd holds nothing for it.
    result, mutations, loaded, _ = _load(tmp_path, HOLDING_BACK["key-empty"], JOB_WITHOUT_SOCKET)
    assert mutations == [] and loaded
    assert "It stays loaded as it is: a bootout would free 127.0.0.1:7002 for any local user to take." in result.stdout
    assert "launchd keeps holding" not in result.stdout


def test_an_unsafe_path_stops_a_loaded_daemon_disabling_it_first(tmp_path):
    # F13: a PATH that another user can modify must not reach the daemon's next start. Disabled
    # before the bootout (review: a bootout that failed left it enabled, for the next boot).
    result, mutations, loaded, outcome = _load(tmp_path, UNSAFE_PATH, JOB_WITH_SOCKET, finish=True)
    assert mutations == ["disable system/com.webspec.gateway.daemon", "bootout system/com.webspec.gateway.daemon"]
    assert not loaded
    assert "stopping com.webspec.gateway.daemon: use directories that only root can modify, or set " \
           "ALLOW_NONROOT_PATH=1" in result.stdout
    assert "nothing holds 127.0.0.1:7002: any local user can take it" in _warnings(result.stderr)
    assert "stays disabled, at boot too" in result.stdout
    assert outcome["STOPPED"] == "1"
    # Review: a run that stopped the daemon exited 0.
    assert result.returncode == 1
    assert "install.sh: com.webspec.gateway.daemon was running, and is stopped and disabled now" in result.stderr


@pytest.mark.parametrize("key", ["key-empty", "key-unusable"])
def test_an_unsafe_path_stops_a_loaded_daemon_whatever_its_key(tmp_path, key):
    # Review: a refused guard.key held the daemon back before gateway.env was read, so a loaded
    # daemon stayed loaded and enabled, and its next start (a crash, a reboot) ran from that PATH.
    result, mutations, loaded, outcome = _load(tmp_path, HOLDING_BACK[key] + UNSAFE_PATH, JOB_WITH_SOCKET,
                                               finish=True)
    assert mutations == ["disable system/com.webspec.gateway.daemon", "bootout system/com.webspec.gateway.daemon"]
    assert not loaded
    assert "stopping com.webspec.gateway.daemon: use directories that only root can modify" in result.stdout
    assert outcome["STOPPED"] == "1"
    assert outcome["KEY_STATE"] == {"key-empty": "1", "key-unusable": "2"}[key]  # the next steps still say so
    assert result.returncode == 1


@pytest.mark.parametrize("listener", ["4242@*:7002", "4242@[::1]:7002", "4242@192.168.1.5:7002"],
                         ids=["wildcard", "ipv6-loopback", "lan-address"])
def test_an_unsafe_path_does_not_hand_the_port_to_a_waiting_listener(tmp_path, listener):
    # Review: a process can listen on *:7002 beside launchd's 127.0.0.1:7002. It gets no connection
    # until launchd lets go of the port, then every one of them: so no bootout while it is there.
    pid, address = listener.split("@")
    result, mutations, loaded, outcome = _load(tmp_path, UNSAFE_PATH, JOB_WITH_SOCKET, finish=True,
                                               FAKE_LSOF_7002=f"1 999 {listener}", FAKE_PS_USER="agent")
    assert mutations == ["disable system/com.webspec.gateway.daemon"]
    assert loaded
    assert f"not booting out com.webspec.gateway.daemon: port 7002 is also held by {pid} (agent, python3.12) " \
           f"on {address}" in _warnings(result.stderr)
    assert (outcome["HELD"], outcome["STOPPED"]) == ("1", "0")
    assert result.returncode == 1


def test_a_bootout_that_fails_leaves_the_daemon_disabled(tmp_path):
    # Review: wait_unloaded gave up before the disable ran, so launchd would have loaded the
    # refused job at the next boot.
    result, mutations, loaded, outcome = _load(tmp_path, UNSAFE_PATH + "UNLOAD_TRIES=4; ", JOB_WITH_SOCKET, finish=True,
                                               FAKE_BOOTOUT_FAILS="1")
    assert mutations == ["disable system/com.webspec.gateway.daemon", "bootout system/com.webspec.gateway.daemon"]
    assert loaded
    assert "is still loaded 1 s after the bootout. It is disabled, so launchd does not load it at the next boot" \
           in _warnings(result.stderr)
    assert result.returncode == 1


def test_a_bootout_is_waited_for_longer_than_launchd_waits_for_the_gateway(tmp_path):
    # launchd sends SIGKILL ExitTimeOut seconds after SIGTERM, and launchctl bootout can return
    # before that ("Operation now in progress"). check_plist holds the plist to EXIT_TIMEOUT.
    result, _ = _source(tmp_path, 'echo "$EXIT_TIMEOUT $UNLOAD_TRIES"')
    exit_timeout, unload_tries = map(int, result.stdout.split())
    assert exit_timeout == _daemon()["ExitTimeOut"]
    assert unload_tries / 4 > exit_timeout


def test_a_first_load_enables_and_bootstraps_the_daemon(tmp_path):
    result, mutations, loaded, outcome = _load(tmp_path, "", None, plist_same=False, finish=True)
    assert result.returncode == 0, result.stderr
    assert mutations == ["enable system/com.webspec.gateway.daemon",
                         f"bootstrap system {INSTALLED_PLIST}"]
    assert loaded and "health new" in result.stdout and outcome["LOADED"] == "1" and outcome["FAILURE"] == ""


def test_a_rerun_with_the_same_plist_restarts_the_gateway_without_releasing_its_port(tmp_path):
    # DP-9: kickstart -k replaces the gateway while launchd keeps holding 127.0.0.1:7002; a
    # bootout would free the port until the bootstrap, for anyone to take.
    result, mutations, loaded, outcome = _load(tmp_path, "", JOB_WITH_SOCKET, plist_same=True, finish=True)
    assert result.returncode == 0, result.stderr
    assert mutations == ["enable system/com.webspec.gateway.daemon",
                         "kickstart -k system/com.webspec.gateway.daemon"]
    # The health check waits for an instance other than the one replaced.
    assert loaded and "health 999" in result.stdout and outcome["LOADED"] == "1"


@pytest.mark.parametrize("job, plist_same", [(JOB_WITH_SOCKET, False), (JOB_WITHOUT_SOCKET, True)],
                         ids=["plist-changed", "loaded-without-the-socket"])
def test_a_job_loaded_from_another_plist_is_reloaded(tmp_path, job, plist_same):
    result, mutations, loaded, outcome = _load(tmp_path, "", job, plist_same=plist_same, finish=True,
                                               FAKE_LSOF_7002="1 999")
    assert result.returncode == 0, result.stderr
    assert mutations == ["bootout system/com.webspec.gateway.daemon", "enable system/com.webspec.gateway.daemon",
                         f"bootstrap system {INSTALLED_PLIST}"]
    assert loaded and "health new" in result.stdout


def test_a_job_is_not_reloaded_while_another_process_listens_on_its_port(tmp_path):
    # From the bootout to the bootstrap nothing holds the port; a listener waiting at another
    # address would take what Caddy forwards in that gap (review).
    result, mutations, loaded, outcome = _load(tmp_path, "", JOB_WITH_SOCKET, plist_same=False, finish=True,
                                               FAKE_LSOF_7002="1 999 4242@*:7002", FAKE_PS_USER="agent")
    assert mutations == []
    assert loaded
    assert "port 7002 is also held by 4242 (agent, python3.12) on *:7002, which would receive what Caddy " \
           "forwards between the bootout of com.webspec.gateway.daemon and its bootstrap" in _warnings(result.stderr)
    assert "not restarting com.webspec.gateway.daemon: another process listens on port 7002" in result.stdout
    assert outcome["HELD"] == "1" and result.returncode == 1


def test_an_address_taken_during_a_reload_keeps_the_daemon_enabled_for_the_next_boot(tmp_path):
    # Another process bound 127.0.0.1:7002 between the bootout and the bootstrap: launchd cannot
    # bind it now. The daemon's own checks passed, so it stays enabled, and launchd loads it at
    # the next boot, before any login.
    result, mutations, loaded, outcome = _load(tmp_path, "", JOB_WITH_SOCKET, plist_same=False,
                                               real=("check_gateway_port",), tools={"lsof": FAKE_LSOF_SEQ},
                                               finish=True, FAKE_DIR=str(tmp_path), FAKE_SEQ_lsof="1,999 4242",
                                               FAKE_PS_USER="agent")
    assert mutations == ["bootout system/com.webspec.gateway.daemon", "enable system/com.webspec.gateway.daemon"]
    assert not loaded
    assert "127.0.0.1:7002 is held by 4242 (agent, python3.12) on 127.0.0.1:7002" in _warnings(result.stderr)
    assert "launchd loads it at the next boot, before any login" in result.stdout
    assert outcome["STOPPED"] == "1" and result.returncode == 1
    assert "This run stopped com.webspec.gateway.daemon" in _source(
        tmp_path, "KEY_STATE=0; STOPPED=1; next_steps")[0].stdout


def test_a_listener_at_another_address_does_not_stop_a_reload(tmp_path):
    # Review: refusing the bootstrap left such a listener every connection. launchd's more
    # specific 127.0.0.1:7002 takes them back, and check_health names the listener.
    result, mutations, loaded, outcome = _load(tmp_path, "", JOB_WITH_SOCKET, plist_same=False,
                                               real=("check_gateway_port",), tools={"lsof": FAKE_LSOF_SEQ},
                                               FAKE_DIR=str(tmp_path), FAKE_SEQ_lsof="1,999 4242@*:7002",
                                               FAKE_PS_USER="agent")
    assert mutations == ["bootout system/com.webspec.gateway.daemon", "enable system/com.webspec.gateway.daemon",
                         f"bootstrap system {INSTALLED_PLIST}"]
    assert loaded and "health new" in result.stdout


def test_a_failed_bootstrap_is_reported(tmp_path):
    result, mutations, loaded, outcome = _load(tmp_path, "", None, plist_same=False, finish=True,
                                               FAKE_BOOTSTRAP_FAILS="1")
    assert mutations == ["enable system/com.webspec.gateway.daemon", f"bootstrap system {INSTALLED_PLIST}"]
    assert "launchctl bootstrap failed" in _warnings(result.stderr)
    assert outcome["LOADED"] == "0" and result.returncode == 1
    assert "install.sh: com.webspec.gateway.daemon could not be loaded" in result.stderr


def test_an_unhealthy_gateway_fails_the_run(tmp_path):
    result, _, _, _ = _load(tmp_path, "check_health() { UNHEALTHY=1; }; ", None, plist_same=False, finish=True)
    assert result.returncode == 1
    assert "install.sh: installed, but the gateway is not healthy" in result.stderr


@non_root
def test_a_dry_run_that_cannot_read_the_key_plans_nothing_for_the_daemon(tmp_path):
    # Review: without root guard.key read as missing, and the plan showed a bootout and a disable
    # of a healthy daemon that a real run would restart in place.
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "guard.key").write_text(HEX)
    etc.chmod(0)
    try:
        result, mutations, loaded, outcome = _load(tmp_path, f"DRY_RUN=1; ETC={etc}; GUARD_KEY={etc}/guard.key; ",
                                                   JOB_WITH_SOCKET, real=("key_state",))
    finally:
        etc.chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert mutations == [] and loaded
    assert not any(line.startswith("+ launchctl") for line in _plan(result))
    assert f"{etc}/guard.key cannot be read without root" in result.stdout
    assert outcome["KEY_STATE"] == "3"
    # The same when the directory can be entered but the key is root's.
    (etc / "guard.key").chmod(0)
    try:
        result, mutations, _, outcome = _load(tmp_path, f"DRY_RUN=1; ETC={etc}; GUARD_KEY={etc}/guard.key; ",
                                              JOB_WITH_SOCKET, real=("key_state",))
    finally:
        (etc / "guard.key").chmod(0o600)
    assert outcome["KEY_STATE"] == "3" and not any(line.startswith("+ launchctl") for line in _plan(result))


@pytest.mark.parametrize("key_state, dev_agent, loaded, held, says, never", [
    (1, "", 0, 0, "- Fill the guard key straight from your password manager", "Rotate the guard key"),
    (2, "", 0, 0, "- Fill the guard key straight from your password manager", "Fix what the warnings above name"),
    (3, "", 0, 0, "- Fill or rotate the guard key", "- Rotate the guard key"),
    (0, "gui/501/com.webspec.gateway", 0, 0, "- Rotate the guard key", "Fill the guard key"),
    (0, "", 0, 0, "- Fix what the warnings above name, then run this installer again", "keeps running"),
    (0, "", 0, 1, "Until then com.webspec.gateway.daemon keeps running as it did before this run", "Fill the guard"),
    (0, "", 1, 0, "- Rotate the guard key", "Fix what the warnings above name"),
], ids=["key-empty", "key-unusable", "key-not-read", "dev-agent-key-filled", "refused-key-filled", "held",
        "loaded"])
def test_next_steps_ask_for_the_key_only_when_it_is_missing(tmp_path, key_state, dev_agent, loaded, held, says,
                                                           never):
    body = f"KEY_STATE={key_state}; DEV_AGENT={dev_agent}; LOADED={loaded}; HELD={held}; next_steps"
    result, _ = _source(tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert says in result.stdout and never not in result.stdout
    # The fill command is there either way: the same command rotates the key.
    assert f"      {FILL_COMMAND}\n" in result.stdout


def test_the_daemons_own_configuration_is_judged_before_other_processes(tmp_path):
    # Review: run the checks of the daemon's own configuration even when a dev agent is loaded, so
    # that a loaded daemon with an unsafe PATH is stopped whatever else holds the run back.
    first, second = _tree(tmp_path, "first", "second")
    result, mutations, _, outcome = _load(first, HOLDING_BACK["dev-agent"] + "key_state() { return 2; }; ", None)
    assert "not loading: /etc/webspec/guard.key does not hold a usable key" in result.stdout
    assert outcome["KEY_STATE"] == "2"
    result, mutations, _, _ = _load(second, HOLDING_BACK["dev-agent"] + UNSAFE_PATH, JOB_WITH_SOCKET)
    assert mutations == ["disable system/com.webspec.gateway.daemon", "bootout system/com.webspec.gateway.daemon"]


@pytest.mark.parametrize("content, message", [("\n", "is empty"), ("short\n", "does not hold a usable key")],
                         ids=["empty", "unusable"])
def test_a_rerun_without_a_usable_key_leaves_the_running_daemon_holding_its_port(tmp_path, content, message):
    # The real key check while a daemon from an earlier run is loaded (finding: a re-run said "not
    # loading: guard.key is empty" and left that daemon running keyless; review: booting it out
    # instead freed its port). It keeps running and says so; it fails closed without a key.
    key = tmp_path / "guard.key"
    key.write_text(content)
    body = f"GUARD_KEY={key}; PY_REAL={sys.executable}; PY_UNSAFE=; "
    result, mutations, loaded, outcome = _load(tmp_path, body, JOB_WITH_SOCKET, real=("key_state",), finish=True)
    assert f"not restarting com.webspec.gateway.daemon: {key} {message}" in result.stdout
    assert "the gateway fails closed" in result.stdout
    assert mutations == [] and loaded
    assert result.returncode == 1


# ── The guard key: what counts as one (C4), how it is filled (F50), who owns it (F51) ──


HEX = hashlib.sha256(b"key-file-content").hexdigest()

KEY_FILES = {
    # usable (0)
    "hex": (HEX, 0), "hex-newline": (HEX + "\n", 0), "bom": ("﻿" + HEX, 0),
    "bom-crlf": ("﻿" + HEX + "\r\n", 0), "zero-width-spaces": ("​" + HEX + "​\n", 0),
    "mixed-invisibles": ("⁠ " + HEX + "　‍", 0), "sixteen": ("sixteen-chars-ok\n", 0),
    # nothing (1)
    "empty": ("", 1), "newline": ("\n", 1), "blanks": ("  \t\r\n", 1), "bom-only": ("﻿", 1),
    "bom-newline": ("﻿\n", 1), "zero-width-space": ("​", 1), "invisibles": ("﻿​ ⁠\n", 1),
    # not a usable key (2)
    "two-lines": (HEX + "\n" + HEX, 2), "two-crlf-lines": (HEX + "\r\n" + HEX + "\r\n", 2),
    "line-separator": ("first line of a passphrase second line", 2),
    "inner-zero-width-space": (HEX[:32] + "​" + HEX[32:], 2), "inner-tab": (HEX[:32] + "\t" + HEX[32:], 2),
    "inner-nul": (HEX[:32] + "\x00" + HEX[32:], 2), "short": ("k" * 15, 2), "short-after-bom": ("﻿" + "k" * 15 + "\n", 2),
    "too-big": ("k" * 4097, 2),
}


def _key_state(tmp_path, content: bytes | None, **env):
    key = tmp_path / "guard.key"
    if content is not None:
        key.write_bytes(content)
    body = f'GUARD_KEY={key}; PY_REAL={env.pop("PY_REAL", sys.executable)}; s=0; key_state || s=$?; echo "state=$s"'
    result, _ = _source(tmp_path, body, **env)
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.parametrize("name", sorted(KEY_FILES))
def test_the_installer_judges_a_key_file_by_the_gateways_rule(tmp_path, name):
    # C4: the installer loads the daemon only for a key that the gateway will accept, and calls
    # a file of invisible characters empty, as the gateway does.
    content, state = KEY_FILES[name]
    result = _key_state(tmp_path, content.encode())
    assert result.stdout.strip() == f"state={state}"
    assert result.stderr == "" and HEX[:12] not in result.stdout and "kkkk" not in result.stdout


def test_a_key_file_that_is_not_utf8_or_missing(tmp_path):
    assert _key_state(tmp_path, b"\xff" * 32).stdout.strip() == "state=2"
    (tmp_path / "guard.key").unlink()
    assert _key_state(tmp_path, None).stdout.strip() == "state=1"


def test_the_installer_and_the_gateway_agree_on_every_key_file(tmp_path, monkeypatch):
    # The same corpus through the gateway's own reader. A gateway that does not apply C4 yet
    # accepts a 15-character key, and then there is nothing to compare.
    monkeypatch.delenv("WEBSPEC_GUARD_KEY", raising=False)
    monkeypatch.delenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", raising=False)
    probe = tmp_path / "probe.key"
    probe.write_text("k" * 15)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(probe))
    try:
        gw_config.get_session_key()
        pytest.skip("the gateway does not apply the C4 key-file rule yet")
    except gw_config.GuardKeyError:
        pass
    for name, (content, state) in sorted(KEY_FILES.items()):
        probe.write_bytes(content.encode())
        try:
            gw_config.get_session_key()
            gateway = 0
        except gw_config.GuardKeyError as exc:
            gateway = 1 if "is empty" in str(exc) else 2
        assert gateway == state, name


def test_key_state_does_not_run_an_interpreter_that_a_dry_run_as_root_may_not_run(tmp_path):
    # select_python's rule holds here too: as root, a Python that others can modify is never run
    # without ALLOW_NONROOT_PYTHON=1. Then only blankness is judged.
    marker = tmp_path / "ran"
    fake = tmp_path / "python3"
    fake.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 11\n')
    fake.chmod(0o755)
    key = tmp_path / "guard.key"
    key.write_text("k" * 15)
    body = f'AS_ROOT=1; PY_UNSAFE={fake}; GUARD_KEY={key}; PY_REAL={fake}; s=0; key_state || s=$?; echo "state=$s"'
    result, _ = _source(tmp_path, body)
    assert result.stdout.strip() == "state=0" and not marker.exists()
    result, _ = _source(tmp_path, "ALLOW_NONROOT_PYTHON=1; " + body)
    assert result.stdout.strip() == "state=2" and marker.exists()


# The installed venv's python as check_guard_key runs it for --fill-key (sudo -u _webspec env -i
# ... python -I -c <probe>): this checkout's gateway, importable as the venv has it installed.
VENV_PYTHON = f"""#!/bin/sh
[ "$1" = -I ] && shift
PYTHONPATH={shlex.quote(str(GATEWAY))} exec {shlex.quote(sys.executable)} -s "$@"
"""
# One that refuses every key, as a gateway with a stricter rule would.
REFUSING_PYTHON = """#!/bin/sh
echo "WEBSPEC_GUARD_KEY_FILE '/etc/webspec/guard.key.new' holds a key this gateway refuses" >&2
exit 1
"""
FAKE_CHOWN = """#!/bin/sh
printf 'chown %s\\n' "$*" >> "$FAKE_LOG"
"""
OLD_KEY = "OLD-KEY-0123456789abcdef\n"


def _fill(tmp_path, producer: str, wait: float | None = None, venv_python: str = VENV_PYTHON,
          env_file: str | None = None):
    """Run --fill-key's fill_key as root would, with producer's output on its standard input, on a
    key file in tmp_path that holds an old key (chown is a fake: only root may give a file to
    root:_webspec). Returns (status, stdout, stderr, key file, its content `wait` seconds in or
    None, the old file's inode, the calls)."""
    etc = tmp_path / "etc"
    etc.mkdir()
    key = etc / "guard.key"
    key.write_text(OLD_KEY)
    key.chmod(0o440)
    inode = key.stat().st_ino
    if env_file is not None:
        (etc / "gateway.env").write_text(env_file)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    _write_tool(venv / "bin", "python", venv_python)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_tool(bin_dir, "sudo", FAKE_SUDO)
    _write_tool(bin_dir, "chown", FAKE_CHOWN)
    log = tmp_path / "calls.log"
    log.touch()
    body = (f"ETC={etc}; GUARD_KEY={key}; ENV_FILE={etc / 'gateway.env'}; VENV={venv}; STATE={tmp_path}; "
            f"PY_REAL={sys.executable}; "
            "record_exists() { return 0; }; job_loaded() { return 0; }; fill_key")
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path),
           "FAKE_LOG": str(log), "DRY_RUN": "0"}
    fill = " ".join(shlex.quote(a) for a in (BASH, "-c", '. "$1"; eval "$2"', "bash", str(INSTALL), body))
    # fill_key starts at once and waits on its standard input while the producer works.
    proc = subprocess.Popen(["/bin/sh", "-c", "{ " + producer + "; } | " + fill], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    during = None
    if wait is not None:
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            during = key.read_text() if key.exists() else None
    out, err = proc.communicate(timeout=60)
    return proc.returncode, out, err, key, during, inode, log.read_text().splitlines()


PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\n-----END OPENSSH PRIVATE KEY-----\n"
FILL_REFUSED = {
    # F50: the gateway reads guard.key on every request, so emptying it, as `op read | tee` did
    # when op failed, broke every guarded request at once.
    "op-fails": ("sh -c 'echo \"[ERROR] You are not currently signed in\" >&2; exit 1'", "there is no key"),
    "nothing": ("true", "there is no key"),
    "blank-line": ("printf '\\n'", "there is no key"),
    # Review: the fill judged only the length of the first line, so these replaced a working key
    # with one the gateway refuses (C4)...
    "spaces-only": ("printf '%20s\\n' ''", "there is no key"),
    "too-short": ("printf 'short\\n'", "does not hold a usable key"),
    "fifteen-and-a-space": ("printf 'abcdefghijklmno \\n'", "does not hold a usable key"),
    "inner-tab": ("printf 'abcdefgh\\tijklmnopq\\n'", "does not hold a usable key"),
    "inner-zero-width-space": ("printf 'abcdefgh\\342\\200\\213ijklmnopq\\n'", "does not hold a usable key"),
    "not-utf8": ("printf '\\377\\376abcdefghijklmnopq\\n'", "does not hold a usable key"),
    "too-big": ("head -c 5000 /dev/zero | tr '\\000' k", "does not hold a usable key"),
    # ...and kept the first line of a secret of several lines: the first line of an SSH key is
    # public, so the guard key became one anyone can compute.
    "pem": (f"printf '%s' {shlex.quote(PEM)}", "does not hold a usable key"),
    "two-keys": (f"printf '%s\\n%s\\n' {HEX} {HEX}", "does not hold a usable key"),
}


@pytest.mark.parametrize("case", sorted(FILL_REFUSED))
def test_a_refused_key_fill_leaves_the_old_key_in_place(tmp_path, case):
    producer, reason = FILL_REFUSED[case]
    rc, out, err, key, _, inode, _ = _fill(tmp_path, producer)
    assert rc == 1, out + err
    assert reason in err and f"{key} is unchanged" in err
    assert key.read_text() == OLD_KEY and key.stat().st_ino == inode
    assert not (key.parent / "guard.key.new").exists()
    assert HEX[:12] not in out + err and "BEGIN OPENSSH" not in out + err


def test_a_key_the_installed_gateway_refuses_is_not_filled(tmp_path):
    # The gateway itself, as the service user, has the last word (GD-5), as before every load.
    rc, out, err, key, _, inode, calls = _fill(tmp_path, f"printf '%s\\n' {HEX}", venv_python=REFUSING_PYTHON)
    assert rc == 1
    assert "holds a key this gateway refuses" in err
    assert f"the installed gateway does not accept it; {key} is unchanged" in err
    assert key.read_text() == OLD_KEY and key.stat().st_ino == inode
    assert not (key.parent / "guard.key.new").exists()


@pytest.mark.parametrize("producer", [f"printf '%s\\n' {HEX}", f"printf '%s' {HEX}", f"printf '  %s\\r\\n' {HEX}"],
                         ids=["newline", "no-newline", "blanks-around"])
def test_a_key_fill_replaces_the_key_in_one_step(tmp_path, producer):
    rc, out, err, key, _, inode, calls = _fill(tmp_path, producer)
    assert rc == 0, out + err
    assert HEX in key.read_text()
    assert stat.S_IMODE(key.stat().st_mode) == 0o440
    # A new file renamed over the old one: a reader sees the old key or the new one, never half.
    assert key.stat().st_ino != inode
    assert not (key.parent / "guard.key.new").exists()
    new = shlex.quote(str(key.parent / "guard.key.new"))
    assert f"chown root:_webspec {key.parent / 'guard.key.new'}" in calls
    # Judged by the installed gateway as the service user, on the new file, before the rename.
    assert any(c.startswith(f"sudo -u _webspec /usr/bin/env -i HOME={tmp_path} PATH=/usr/bin:/bin "
                            f"WEBSPEC_GUARD_KEY_FILE={key.parent / 'guard.key.new'} ") for c in calls)
    assert f"+ mv -f {new} {shlex.quote(str(key))}" in out
    assert "the new key is in effect now" in out
    assert HEX[:12] not in out + err


def test_a_key_fill_says_when_gateway_env_overrides_the_key(tmp_path):
    # A WEBSPEC_GUARD_KEY in gateway.env wins over guard.key, so the new key would change
    # nothing: the fill says so instead of "in effect now" (review of the deployment guide).
    rc, out, err, key, _, _, _ = _fill(tmp_path, f"printf '%s\\n' {HEX}",
                                       env_file="WEBSPEC_DOMAIN='example.com'\nWEBSPEC_GUARD_KEY='" + "b" * 64 + "'\n")
    assert rc == 0, out + err
    assert HEX in key.read_text()
    assert "sets WEBSPEC_GUARD_KEY, which the gateway uses instead of" in err
    assert "in effect now" not in out + err
    assert "b" * 16 not in out + err


def test_a_key_fill_ignores_a_gateway_env_without_the_key(tmp_path):
    rc, out, err, *_ = _fill(tmp_path, f"printf '%s\\n' {HEX}", env_file="WEBSPEC_DOMAIN='example.com'\n")
    assert rc == 0, out + err
    assert "the new key is in effect now" in out and "WEBSPEC_GUARD_KEY" not in err


def test_a_slow_key_fill_keeps_the_old_key_until_the_new_one_is_whole(tmp_path):
    # tee truncated first and wrote last: while op waited for Touch ID, the key was empty.
    rc, out, err, key, during, _, _ = _fill(tmp_path, f"sleep 2; printf '%s\\n' {HEX}", wait=0.5)
    assert during == OLD_KEY
    assert rc == 0, err
    assert key.read_text() == HEX + "\n"


def test_a_key_fill_refuses_a_key_typed_at_a_terminal(tmp_path):
    # Typed keys end up on screen and in the terminal's scrollback: pipe it in.
    import pty
    controller, terminal = pty.openpty()
    try:
        result = subprocess.run([BASH, "-c", '. "$1"; fill_key', "bash", str(INSTALL)], stdin=terminal,
                                capture_output=True, text=True, timeout=60)
    finally:
        os.close(controller)
        os.close(terminal)
    assert result.returncode == 1
    assert "--fill-key reads the key from standard input: pipe it in from your password manager" in result.stderr


def test_a_key_fill_needs_an_installation(tmp_path):
    result = subprocess.run([BASH, "-c", '. "$1"; ETC="$2"; fill_key', "bash", str(INSTALL), str(tmp_path / "none")],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert f"nothing is installed here yet: run sudo {INSTALL} first" in result.stderr


def test_fill_key_has_no_dry_run(dry_run):
    result, calls = dry_run("--fill-key")
    assert result.returncode == 1 and "--fill-key has no dry run" in result.stderr
    assert calls == []


@non_root
def test_fill_key_without_root_changes_nothing(dry_run):
    result, calls = dry_run("--fill-key", DRY_RUN=None)
    assert result.returncode == 1
    assert f"must be run as root: <password manager> | sudo {INSTALL} --fill-key" in result.stderr
    assert calls == []  # the poisoned id, dirname and uname never ran


def test_guard_key_is_converged_to_root_and_the_service_group(tmp_path):
    # F51: an existing key file (as an earlier version left it, _webspec:_webspec 0400) gets root's
    # ownership, the group's read access and no ACL; the service user can no longer change it.
    etc = tmp_path / "etc"
    etc.mkdir()
    for name in ("guard.key", "config.json", "gateway.env", "allowed_signers"):
        (etc / name).write_text("")
    body = (f"DRY_RUN=1; ETC={etc}; CONFIG={etc}/config.json; ENV_FILE={etc}/gateway.env; GUARD_KEY={etc}/guard.key; "
            f"SIGNERS={etc}/allowed_signers; STATE={tmp_path}/state; LOGDIR={tmp_path}/log; install_files")
    result, _ = _source(tmp_path, body)
    assert result.returncode == 0, result.stderr
    plan = _plan(result)
    key = shlex.quote(str(etc / "guard.key"))
    assert [ln for ln in plan if ln.endswith(" " + key)] == [
        f"+ chown root:_webspec {key}", f"+ chmod 0440 {key}", f"+ chmod -N {key}"]
    assert f"+ chmod -N {shlex.quote(str(etc / 'gateway.env'))}" in plan


# ── What the daemon needs from the gateway ──


def test_gateway_loads_the_guard_key_from_the_file_the_daemon_names(tmp_path, monkeypatch):
    key = tmp_path / "guard.key"
    key.write_text("ab" * 32 + "\n")
    key.chmod(0o400)
    monkeypatch.delenv("WEBSPEC_GUARD_KEY", raising=False)
    monkeypatch.delenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", raising=False)
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_FILE", str(key))
    assert gw_config.get_session_key() == bytes.fromhex("ab" * 32)


@non_root
def test_installed_allowed_signers_counts_as_no_approvers_until_one_is_added(dry_run, tmp_path, monkeypatch):
    # AP-7: with the file as install.sh creates it, level-4 requests get 503 approval_unavailable,
    # not a challenge that no key could sign.
    result, _ = dry_run(FAKE_DEV_AGENT="0")
    signers = tmp_path / "allowed_signers"
    signers.write_text(_written(result, "allowed_signers"))
    monkeypatch.setenv("WEBSPEC_APPROVERS_FILE", str(signers))
    monkeypatch.setenv("WEBSPEC_SSH_KEYGEN", "/usr/bin/ssh-keygen")
    assert approval.approvers_available() is False
