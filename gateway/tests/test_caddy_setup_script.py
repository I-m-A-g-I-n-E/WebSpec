"""gateway/tools/setup-caddy.sh, which root runs.

- F7 (DP-1, DP-4): it refuses a checkout, or an installed gateway, that a user other than root can
  change, before anything else runs; its own PATH holds system directories only.
- F9: it plans first, refuses to stop serving hosts unless ALLOW_SHRINK=1, and checks afterwards
  that every host Caddy served is served as well as before.
- F27: replacing an earlier setup, it warns about the unfiltered entries in the journal.
- F29: it stops cloudflared.service while the ports change hands, and starts it again only once
  systemd holds every listener; otherwise it names who holds them.
- F30: a replaced unit that this repository did not ship is kept; hints use commands that exist.
- F31, F36: the advice for a public domain restarts the gateway, and needs no /etc/webspec.
- P2: it never drops the socket unit's [::1] lines, and removes the drop-in an earlier version
  wrote; caddy-webspec.service does not start a Caddyfile that binds [::1] on a kernel without
  IPv6; the units and the header say to run the script again after an IPv6 change at boot.
- A configuration that failed is never left in place: when the new one cannot be written, does
  not validate where it is installed, cannot be installed with its caddy, or the script stops on
  the way, and when Caddy does not reload it, the previous files go back as they were, the very
  same files; after a failed reload, Caddy reloads them. Once the new caddy is in place, or the
  units change, a failure or a stop leaves the new one, which validated, and says what is on
  disk; what cannot be put back is named from what moved.

The helpers are taken out of the script and run in bash, so that the tests run its own code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
SCRIPT = GATEWAY_DIR / "tools" / "setup-caddy.sh"
UNIT_DIR = GATEWAY_DIR / "deploy" / "linux"
TEXT = SCRIPT.read_text()
# The script without its comment lines: where commands run, not where the header mentions them.
CODE = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


def _function(name: str) -> str:
    """The definition of shell function ``name`` in the script: one line, or ending at a `}` in column 0."""
    m = (re.search(rf"^{name}\(\) {{ [^\n]*; }}$", TEXT, re.M)
         or re.search(rf"^{name}\(\) {{.*?^}}$", TEXT, re.S | re.M))
    assert m, f"setup-caddy.sh defines no {name}()"
    return m.group(0)


def _bash(code: str, *, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, "-c", "set -euo pipefail\n" + code], capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], **(env or {})}, timeout=60)


# ── F30: a replaced unit that this repository did not ship is kept ──


def _shipped() -> set[str]:
    m = re.search(r'^SHIPPED_UNITS="\n(.*?)^"$', TEXT, re.S | re.M)
    assert m
    return set(m.group(1).split())


def test_the_units_this_repository_ships_are_listed():
    shipped = _shipped()
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in shipped)
    for unit in ("caddy-webspec.service", "caddy-webspec.socket"):
        digest = hashlib.sha256((UNIT_DIR / unit).read_bytes()).hexdigest()
        assert digest in shipped, f"add the sha256 of {unit} to SHIPPED_UNITS in setup-caddy.sh: {digest}"


def test_every_unit_in_the_history_is_listed():
    # An installed unit equal to any shipped version is replaced without a copy; others are kept.
    git = shutil.which("git")
    if git is None or subprocess.run([git, "-C", str(GATEWAY_DIR), "rev-parse"], capture_output=True).returncode:
        pytest.skip("not a git checkout")
    shipped = _shipped()
    for unit in ("caddy-webspec.service", "caddy-webspec.socket"):
        path = f"gateway/deploy/linux/{unit}"
        commits = subprocess.run([git, "-C", str(GATEWAY_DIR), "log", "--format=%H", "--", path],
                                 capture_output=True, text=True, check=True).stdout.split()
        for commit in commits:
            blob = subprocess.run([git, "-C", str(GATEWAY_DIR), "show", f"{commit}:{path}"], capture_output=True)
            if blob.returncode == 0:
                assert hashlib.sha256(blob.stdout).hexdigest() in shipped, f"{unit} at {commit[:7]}"


def test_a_replaced_unit_is_compared_before_it_is_installed():
    keep = TEXT.index('keep_unless_shipped "${UNIT_DIR}/${UNIT}"')
    assert keep < TEXT.index('install_if_changed "${UNIT_SRC_DIR}/${UNIT}"')
    assert 'cp -p -- "$1" "$1.bak.${STAMP}"' in _function("keep_unless_shipped")


# ── F7: who can change what root runs ──


def test_root_runs_nothing_from_the_callers_path():
    assert re.search(r"^PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\nexport PATH$",
                     TEXT, re.M)
    assert TEXT.index("export PATH") < TEXT.index("changeable_by_others()")


def test_the_checkout_is_checked_before_anything_runs_from_it():
    check = CODE.index('untrusted="$(changeable_by_others "$GATEWAY_DIR"')
    for later in ("\ncd /\n", "py listeners", "py domain", "py plan", "systemctl ", "install -", "useradd"):
        assert check < CODE.index(later), later
    venv_check = CODE.index('changeable_by_others -L "$VENV"')
    assert check < venv_check < CODE.index("PYTHON=$VENV_PYTHON") < CODE.index("py listeners")


def test_usage_says_to_run_from_a_root_owned_clone():
    header = TEXT[:TEXT.index("set -euo pipefail")]
    assert "# Usage: sudo git clone https://github.com/I-m-A-g-I-n-E/WebSpec.git /opt/webspec/src" in header
    assert "#        sudo bash /opt/webspec/src/gateway/tools/setup-caddy.sh" in header
    assert "sudo git clone https://github.com/I-m-A-g-I-n-E/WebSpec.git /opt/webspec/src" in _function("refuse_checkout")


@needs_bash
def test_a_checkout_others_can_change_is_refused_before_anything_runs(tmp_path):
    # A copy of gateway/ owned by the test's user stands for the agent's working tree. As that
    # user, the script must also say it needs root; as root (the Linux runner), the copy is
    # root's, and the directories above it (the world-writable /tmp) are what refuse it.
    copy = tmp_path / "WebSpec"
    shutil.copytree(GATEWAY_DIR, copy / "gateway", ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    marker = tmp_path / "ran"
    # Were anything after the check to run, this unit edit would leave a mark (the F7 repro).
    unit = copy / "gateway" / "deploy" / "linux" / "caddy-webspec.service"
    unit.write_text(unit.read_text().replace("ExecStart=", f'ExecStartPre=+/bin/sh -c "touch {marker}"\nExecStart=', 1))
    result = subprocess.run([BASH, str(copy / "gateway" / "tools" / "setup-caddy.sh")], capture_output=True,
                            text=True, timeout=120, cwd=tmp_path)
    assert result.returncode == 1
    assert f"refusing to run from {copy.resolve()}: users other than root can change it" in result.stderr
    assert "sudo git clone https://github.com/I-m-A-g-I-n-E/WebSpec.git /opt/webspec/src" in result.stderr
    assert "=== WebSpec Caddy setup ===" not in result.stdout and not marker.exists()
    if os.geteuid() != 0:
        assert "setup-caddy: run as root" in result.stderr
        assert str(unit.resolve()) in result.stderr or "(and " in result.stderr


# find's `-uid 0` also accepts the test's own user here, so that a tree the test makes stands
# for one that root owns; everything else about the script's code is unchanged.
AS_ROOTS_OWN = r"""find() {
    local i me all=("$@") args=()
    me=$(id -u)
    for ((i = 0; i < ${#all[@]}; i++)); do
        if [ "${all[i]}" = -uid ] && [ "${all[i + 1]:-}" = 0 ]; then
            args+=("(" -uid 0 -o -uid "$me" ")")
            i=$((i + 1))
        else
            args+=("${all[i]}")
        fi
    done
    command find "${args[@]}"
}
"""


def _listed(tree: Path, *args: str) -> set[str]:
    run = _bash(AS_ROOTS_OWN + _function("changeable_by_others") + "\nchangeable_by_others " +
                " ".join(f'"{a}"' for a in args))
    assert run.returncode == 0, run.stderr
    return {line for line in run.stdout.splitlines() if line.startswith(str(tree))}


def _as_root_makes_them(*paths: Path) -> None:
    """Give files and directories the modes root's umask 022 would, whatever the suite's umask is
    (002 for a user with a private group would make each of them group-writable)."""
    for path in paths:
        path.chmod(0o755 if path.is_dir() else 0o644)


@needs_bash
def test_changeable_by_others_lists_links_and_what_others_can_write(tmp_path):
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "file").write_text("x")
    _as_root_makes_them(tree, tree / "sub", tree / "sub" / "file")
    (tree / "group-writable").write_text("x")
    (tree / "group-writable").chmod(0o664)
    (tree / "open").mkdir()
    (tree / "open").chmod(0o777)
    (tree / "link").symlink_to("/bin/sh")
    # A link could point anywhere; what others can write, they can change.
    assert _listed(tree, str(tree)) == {str(tree / "link"), str(tree / "group-writable"), str(tree / "open")}
    # With -L, a link counts by what it leads to: a venv links its python to the system's.
    assert _listed(tree, "-L", str(tree)) == {str(tree / "group-writable"), str(tree / "open")}


@needs_bash
def test_changeable_by_others_checks_every_directory_above(tmp_path):
    tree = tmp_path / "shared" / "checkout"
    tree.mkdir(parents=True)
    _as_root_makes_them(tree)
    (tmp_path / "shared").chmod(0o777)  # whoever can swap an entry here can swap the tree
    try:
        assert _listed(tmp_path, str(tree)) == {str(tmp_path / "shared")}
    finally:
        (tmp_path / "shared").chmod(0o755)


@pytest.fixture
def clean_dir(tmp_path):
    """A new directory whose parents pass changeable_by_others (with AS_ROOTS_OWN).

    tmp_path where its parents do (macOS); else one in the home directory, as on Linux tmp_path is
    under the world-writable /tmp, which the script rightly refuses whatever is below it.
    """
    for base in (tmp_path, Path.home()):
        try:
            candidate = Path(tempfile.mkdtemp(prefix="webspec-venv-test.", dir=base))
        except OSError:
            continue
        if _listed(Path("/"), str(candidate)) == set():
            yield candidate
            shutil.rmtree(candidate, ignore_errors=True)
            return
        shutil.rmtree(candidate, ignore_errors=True)
    pytest.skip("no directory here whose parents only root (or this user) can change")


@needs_bash
def test_a_venv_others_can_change_is_never_run(clean_dir):
    # The script's own lines that check /opt/webspec/venv, on a venv in a directory of the test's.
    block = TEXT[TEXT.index('if [ -x "$VENV_PYTHON" ]; then'):TEXT.index("    PYTHON=$VENV_PYTHON")]
    venv = clean_dir / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to("/bin/sh")
    module = venv / "lib" / "site-packages" / "webspec" / "caddy.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    _as_root_makes_them(venv, venv / "bin", *module.parents[:3], module)
    code = "\n".join([AS_ROOTS_OWN, _function("die"), _function("changeable_by_others"),
                      f'VENV="{venv}"', 'VENV_PYTHON="${VENV}/bin/python"', block + "    echo accepted\nfi"])
    run = _bash(code)
    assert run.returncode == 0 and run.stdout == "accepted\n", run.stderr
    module.chmod(0o664)
    run = _bash(code)
    assert run.returncode == 1 and "accepted" not in run.stdout
    assert f"refusing to run the gateway installed in {venv}: users other than root can change it" in run.stderr
    assert str(module) in run.stderr


def _prebuilt_check(prebuilt: Path | str) -> subprocess.CompletedProcess:
    """The script's own lines that read WEBSPEC_CADDY_BINARY, run on ``prebuilt``."""
    block = TEXT[TEXT.index('PREBUILT=""\n'):TEXT.index("\ncd /\n")]
    code = "\n".join([AS_ROOTS_OWN, _function("die"), _function("changeable_by_others"), _function("absolute"),
                      block, 'echo "accepted ${PREBUILT}"'])
    return _bash(code, env={"WEBSPEC_CADDY_BINARY": str(prebuilt)})


@needs_bash
def test_a_prebuilt_caddy_others_can_change_is_refused(clean_dir):
    # Root installs it as /usr/local/bin/caddy, and webspec-ctl runs that: the agent must not be
    # able to swap it, in the file or in any directory above it, as written or behind a link.
    bindir = clean_dir / "bin"
    bindir.mkdir()
    caddy = bindir / "caddy"
    caddy.write_text("#!/bin/sh\n")
    _as_root_makes_them(bindir, caddy)
    run = _prebuilt_check(caddy)
    assert run.returncode == 0 and run.stdout == f"accepted {caddy}\n", run.stderr
    caddy.chmod(0o775)
    run = _prebuilt_check(caddy)
    assert run.returncode == 1 and "accepted" not in run.stdout
    assert f"refusing the prebuilt caddy {caddy} (WEBSPEC_CADDY_BINARY): users other than root can change it" \
           in run.stderr and str(caddy) in run.stderr.splitlines()[1]
    assert "sudo install -o root -g root -m 0755 ./caddy /root/caddy" in run.stderr
    caddy.chmod(0o755)
    # A link in a directory only root can change, to a file in one that others can.
    shared = clean_dir / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    (shared / "caddy").write_text("#!/bin/sh\n")
    _as_root_makes_them(shared / "caddy")
    (bindir / "linked").symlink_to(shared / "caddy")
    run = _prebuilt_check(bindir / "linked")
    assert run.returncode == 1 and str(shared) in run.stderr
    run = _prebuilt_check(clean_dir / "missing")
    assert run.returncode == 1 and "is not a file" in run.stderr


@needs_bash
def test_a_prebuilt_caddy_in_the_agents_home_is_refused(tmp_path):
    # The reviewer's repro: /home/agent/caddy. Here the test user's file outside a clean tree.
    caddy = tmp_path / "caddy"
    caddy.write_text("#!/bin/sh\ntouch ran\n")
    caddy.chmod(0o755)
    run = _bash("\n".join([_function("die"), _function("changeable_by_others"), _function("absolute"),
                           TEXT[TEXT.index('PREBUILT=""\n'):TEXT.index("\ncd /\n")]]),
                env={"WEBSPEC_CADDY_BINARY": str(caddy)})
    if os.geteuid() != 0:  # as root, tmp_path's files are root's; /tmp above them still refuses it
        assert str(caddy) in run.stderr
    assert run.returncode == 1 and "refusing the prebuilt caddy" in run.stderr


def test_root_never_runs_the_new_caddy():
    # It is copied or built as root, and run only through as_caddy (runuser -u caddy), once that
    # user exists: list-modules, version and validate alike.
    assert 'runuser -u caddy -- env "${CADDY_ENV[@]}" "$@"' in _function("as_caddy")
    runs = list(re.finditer(r'"\$\{(?:WORK|STAGE)\}/caddy" (?:list-modules|version|validate|run|adapt)\b', CODE))
    assert len(runs) == 2  # list-modules and version; validate goes through validate()
    for m in runs:
        assert CODE[:m.start()].endswith("as_caddy "), CODE[max(0, m.start() - 60):m.end()]
    assert re.search(r'^\s+as_caddy "\$1" validate --config "\$2"', _function("validate"), re.M)
    useradd = CODE.index("useradd --system")
    assert useradd < CODE.index('as_caddy "${STAGE}/caddy" list-modules') < CODE.index('as_caddy "${STAGE}/caddy" version')
    check = CODE.index('changeable_by_others -L "$PREBUILT"')
    assert check < CODE.index('cp -- "$PREBUILT"')


@needs_bash
def test_root_owned_system_files_are_not_listed():
    for path in ("/usr/bin/env", "/bin/sh"):
        if Path(path).exists() and not Path(path).is_symlink():
            run = _bash(_function("changeable_by_others") + f'\nchangeable_by_others "{path}"')
            assert run.returncode == 0 and run.stdout == "", run.stdout


# ── F29: the listeners, and who holds them ──


SS = """LISTEN 0 4096 127.0.0.1:7001 0.0.0.0:* users:(("caddy",pid=622,fd=3),("systemd",pid=1,fd=52))
LISTEN 0 4096 127.0.0.1:7003 0.0.0.0:* users:(("python3",pid=1722,fd=3))
LISTEN 0 4096 [::1]:7001 [::]:* users:(("systemd",pid=1,fd=73))
LISTEN 0 4096 127.0.0.2:7001 0.0.0.0:* users:(("python3",pid=1723,fd=4))"""


def _listeners(ss: str, expected: str) -> subprocess.CompletedProcess:
    # ps stands in for the real one: pid 1722 has a name with a terminal escape in it.
    fake_ps = r"""ps() {
    case "$*" in
        *"comm= -p 1722") printf 'evil\033]0;x\n' ;;
        *"comm= -p "*) echo python3 ;;
        *"user= -p "*) echo human ;;
    esac
}"""
    code = "\n".join([fake_ps, _function("owners"), _function("unheld_listeners"),
                      f"LISTENING='{ss}'", f"EXPECTED_LISTENERS='{expected}'", "unheld_listeners"])
    return _bash(code)


@needs_bash
def test_each_listener_systemd_does_not_hold_is_named_with_its_holder():
    run = _listeners(SS, "127.0.0.1:7001 127.0.0.1:7003 [::1]:7001 [::1]:7003")
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "127.0.0.1:7003 is held by evil??0?x (pid 1722, user human)",  # no escape reaches the terminal
        "nothing listens on [::1]:7003",
    ]


@needs_bash
def test_a_listener_at_another_address_of_the_port_does_not_count():
    # 127.0.0.2:7001 receives none of cloudflared's traffic, which goes to 127.0.0.1:7001.
    run = _listeners(SS, "127.0.0.1:7001 [::1]:7001")
    assert run.returncode == 0 and run.stdout == ""


@needs_bash
def test_systemd_holding_one_address_of_a_port_does_not_cover_another():
    # systemd lost 127.0.0.1:7001 (a daemon-reload) and another process took it: [::1]:7001,
    # still systemd's, must not make that look held.
    ss = """LISTEN 0 4096 127.0.0.1:7001 0.0.0.0:* users:(("python3",pid=1723,fd=4))
LISTEN 0 4096 [::1]:7001 [::]:* users:(("caddy",pid=622,fd=5),("systemd",pid=1,fd=73))"""
    run = _listeners(ss, "127.0.0.1:7001 [::1]:7001")
    assert run.stdout.splitlines() == ["127.0.0.1:7001 is held by python3 (pid 1723, user human)"]


HELD = """LISTEN 0 4096 127.0.0.1:7001 0.0.0.0:* users:(("caddy",pid=622,fd=3),("systemd",pid=1,fd=52))
LISTEN 0 4096 127.0.0.1:7003 0.0.0.0:* users:(("systemd",pid=1,fd=53))
LISTEN 0 4096 [::1]:7001 [::]:* users:(("systemd",pid=1,fd=73))
LISTEN 0 4096 [::1]:7003 [::]:* users:(("systemd",pid=1,fd=74))"""
DUAL = "127.0.0.1:7001 127.0.0.1:7003 [::1]:7001 [::1]:7003"


@needs_bash
@pytest.mark.parametrize("active,ss,held", [
    (True, HELD, True),
    (False, HELD, False),  # the socket unit is not active
    (True, "\n".join(HELD.splitlines()[:3]), False),  # it lost [::1]:7003 (a daemon-reload)
    (True, HELD.replace('("systemd",pid=1,fd=53)', '("python3",pid=1722,fd=3)'), False),  # another holds :7003
])
def test_systemd_holds_listeners_needs_every_listener_in_systemds_hands(active, ss, held):
    # The F29 gate: an active socket unit is not enough, each listener must be systemd's.
    code = "\n".join([
        f"systemctl() {{ [ \"$*\" = 'is-active --quiet caddy-webspec.socket' ] && {'true' if active else 'false'}; }}",
        f"ss() {{ printf '%s\\n' '{ss}'; }}",
        "ps() { echo python3; }",
        _function("read_listeners"), _function("owners"), _function("unheld_listeners"),
        _function("systemd_holds_listeners"),
        "SOCKET=caddy-webspec.socket", f"EXPECTED_LISTENERS='{DUAL}'",
        "if systemd_holds_listeners; then echo held; else echo not held; fi",
    ])
    run = _bash(code)
    assert run.returncode == 0 and run.stdout == ("held\n" if held else "not held\n"), run.stderr


# Stand-ins for systemd, run inside the switch's own program. State lives in files under $STATE:
# active units, the systemctl calls in order, and what ss shows once the socket unit has started.
SWITCH_STUBS = r"""
systemctl() {
    echo "$*" >> "$STATE/calls"
    case "$1" in
        is-active) [ -e "$STATE/active-${3:-$2}" ] ;;
        stop) shift; for u in "$@"; do rm -f "$STATE/active-$u"; done ;;
        start) touch "$STATE/active-$2" ;;
    esac
}
ss() { if [ -e "$STATE/active-caddy-webspec.socket" ]; then cat "$STATE/ss"; fi; }
journalctl() { echo "(journal of $*)"; }
ps() { case "$*" in *comm=*) echo python3 ;; *) echo human ;; esac; }
"""

# systemd-run as far as the switch can tell: its program runs in a clean environment, with only
# what it carries and the stand-ins above, its output in the file StandardOutput names.
FAKE_SYSTEMD_RUN = r"""
systemd-run() {
    local arg log=""
    printf '%s\n' "$@" > "$STATE/systemd-run"
    for arg in "$@"; do
        case "$arg" in --property=StandardOutput=file:*) log="${arg#--property=StandardOutput=file:}" ;; esac
    done
    env -i /bin/bash -c '. "$1"; . "$2"' _ "$STATE/stubs.sh" "${!#}" >"$log" 2>&1
}
mktemp() { command mktemp -d "$STATE/cutover.XXXXXX"; }
"""


# What step 5 says is on disk when a failure leaves the new configuration in place (ON_DISK).
SWITCH_ON_DISK = "On disk: the new configuration, and the units this repository ships"


def _switch(tmp_path: Path, ss: str, tunnel_active: bool = True, extra: str = "",
            stops: bool = False) -> subprocess.CompletedProcess:
    """run_cutover with the stand-ins above, and ``extra``, which may replace some of them.

    ``stops``: the script's cleanup runs at exit, with what step 5 leaves it to say (ON_STOP).
    """
    state = tmp_path / "state"
    state.mkdir()
    (state / "stubs.sh").write_text(f"STATE='{state}'\n" + SWITCH_STUBS)
    (state / "ss").write_text(ss + "\n")
    if tunnel_active:
        (state / "active-cloudflared.service").touch()
    (state / "active-caddy-webspec.socket").touch()  # active, but with listeners it lost
    names = ("die", "warn", "unit_failed", "read_listeners", "owners", "unheld_listeners",
             "systemd_holds_listeners", "bind_failed", "cutover", "run_cutover")
    cleanup = [_function("cleanup"), f"WORK='{tmp_path}/work' STAGE='' PREVIOUS='' put_back_on_exit=0 binary_changed=0",
               'ON_STOP="${ON_DISK}. Run this script again."', "trap cleanup EXIT"] if stops else []
    # The script itself asks systemd too (is the tunnel up?), through the same stand-ins.
    code = "\n".join([f"STATE='{state}'", f". '{state}/stubs.sh'", FAKE_SYSTEMD_RUN, extra,
                      *(_function(n) for n in names),
                      "SOCKET=caddy-webspec.socket UNIT=caddy-webspec.service TUNNEL=cloudflared.service",
                      f"EXPECTED_LISTENERS='{DUAL}' CADDY_PORT=7001 DIRECT_PORT=7003 tunnel_stopped=0",
                      f"ON_DISK='{SWITCH_ON_DISK}'", *cleanup,
                      "STAMP=20261006-120000", 'CUTOVER_UNIT="webspec-caddy-cutover-${STAMP}"', "run_cutover",
                      'ON_STOP=""'])  # as the script does once it has finished
    return _bash(code)


@needs_bash
def test_the_switch_stops_the_tunnel_until_systemd_holds_every_listener(tmp_path):
    run = _switch(tmp_path, HELD)
    assert run.returncode == 0, run.stderr
    state = tmp_path / "state"
    assert (state / "calls").read_text().splitlines() == [
        "is-active --quiet cloudflared.service",  # the script: should it warn of the switch?
        "is-active --quiet cloudflared.service", "stop cloudflared.service",  # the switch, as a unit
        "stop caddy-webspec.service caddy-webspec.socket", "start caddy-webspec.socket",
        "is-active --quiet caddy-webspec.socket", "start cloudflared.service", "start caddy-webspec.service"]
    lines = run.stdout.splitlines()
    assert lines[0].startswith("The ports change hands: cloudflared.service stops until systemd holds every")
    assert "Run this script again once you are back." in lines[3]
    assert lines[4:] == [
        "Stopped cloudflared.service while systemd takes the ports over",
        "Started cloudflared.service again: systemd holds every listener",
        "Started caddy-webspec.socket and caddy-webspec.service"]
    # A transient unit of its own, which outlives this script; its directory goes once it succeeded.
    args = (state / "systemd-run").read_text().splitlines()
    assert args[:2] == ["--unit=webspec-caddy-cutover-20261006-120000",
                        "--description=WebSpec: systemd takes Caddy's listeners over"]
    assert {"--collect", "--wait", "--quiet"} <= set(args) and args[-2] == "/bin/bash"
    assert not list(state.glob("cutover.*"))


@needs_bash
def test_the_switch_leaves_the_tunnel_stopped_while_another_process_holds_a_listener(tmp_path):
    run = _switch(tmp_path, HELD.replace('("systemd",pid=1,fd=53)', '("python3",pid=1722,fd=3)'))
    assert run.returncode == 1
    state = tmp_path / "state"
    assert "start cloudflared.service" not in (state / "calls").read_text()
    assert "127.0.0.1:7003 is held by python3 (pid 1722, user human)" in run.stderr
    assert "cloudflared.service was stopped for the switch and stays stopped" in run.stderr
    kept = list(state.glob("cutover.*"))
    # What the switch left on disk, and what to do: the new configuration stays (step 5).
    assert len(kept) == 1 and (f"its output, above, is kept in {kept[0]}/log. {SWITCH_ON_DISK}. Fix the cause, "
                               "then run this script again.") in run.stderr
    assert "stays stopped" in (kept[0] / "log").read_text()


def _switch_unseen(tmp_path: Path, still_running: bool) -> subprocess.CompletedProcess:
    """run_cutover where systemd-run fails without the switch's output: it still runs, or never reported."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "stubs.sh").write_text(f"STATE='{state}'\n" + SWITCH_STUBS)
    if still_running:
        (state / "active-webspec-caddy-cutover-20261006-120000.service").touch()
    names = ("die", "warn", "unit_failed", "read_listeners", "owners", "unheld_listeners",
             "systemd_holds_listeners", "bind_failed", "cutover", "run_cutover")
    return _bash("\n".join([f"STATE='{state}'", f". '{state}/stubs.sh'",
                            'mktemp() { command mktemp -d "$STATE/cutover.XXXXXX"; }',
                            *(_function(n) for n in names), "systemd-run() { return 143; }",
                            "SOCKET=caddy-webspec.socket UNIT=caddy-webspec.service TUNNEL=cloudflared.service",
                            f"EXPECTED_LISTENERS='{DUAL}' CADDY_PORT=7001 DIRECT_PORT=7003 tunnel_stopped=0",
                            f"ON_DISK='{SWITCH_ON_DISK}'",
                            "STAMP=20261006-120000", 'CUTOVER_UNIT="webspec-caddy-cutover-${STAMP}"', "run_cutover"]))


@needs_bash
def test_a_switch_that_outlives_systemd_run_is_not_called_failed(tmp_path):
    # systemd-run stopped (a signal of its own) while the unit goes on: say so, not that it failed.
    run = _switch_unseen(tmp_path, still_running=True)
    assert run.returncode == 1
    assert "lost sight of the switch, which still runs as webspec-caddy-cutover-20261006-120000" in run.stderr
    assert f"/log. {SWITCH_ON_DISK}. Run this script again once the switch has finished." in run.stderr
    assert "failed" not in run.stderr


@needs_bash
def test_a_switch_that_never_reported_back_says_what_is_on_disk(tmp_path):
    run = _switch_unseen(tmp_path, still_running=False)
    assert run.returncode == 1
    assert "the switch (webspec-caddy-cutover-20261006-120000) did not run, or did not report back: see " \
           "journalctl -u webspec-caddy-cutover-20261006-120000 and " in run.stderr
    assert f". {SWITCH_ON_DISK}. Once the journal shows that the switch has ended, run this script again." in run.stderr


@needs_bash
@pytest.mark.parametrize("failure", ["mktemp", "write", "cut short"])
def test_a_switch_that_cannot_be_prepared_changes_nothing(tmp_path, failure):
    # /run is full, or the switch's program cannot be written, or not in full (declare -f fails
    # half-way): nothing is announced, nothing stops, nothing runs, and the message says what is
    # on disk. A program cut short would run without the functions it calls.
    extra = {"mktemp": 'mktemp() { echo "mktemp: No space left on device" >&2; return 1; }',
             "write": 'mktemp() { echo "$STATE/gone/cutover.1"; }',
             "cut short": 'declare() { if [ "$1" = -f ]; then echo "declare: write error" >&2; return 1; fi; '
                          'builtin declare "$@"; }'}[failure]
    run = _switch(tmp_path, HELD, extra=extra)
    state = tmp_path / "state"
    assert run.returncode == 1
    where = "under /run" if failure == "mktemp" else \
        f"in {state / 'gone' / 'cutover.1'}" if failure == "write" else f"in {next(state.glob('cutover.*'))}"
    assert (f"setup-caddy: could not prepare the switch {where} (see above); nothing was switched. {SWITCH_ON_DISK}. "
            "Fix the cause, then run this script again.") in run.stderr
    assert "The ports change hands" not in run.stdout and not (state / "systemd-run").exists()
    assert not (state / "calls").exists()  # systemd was not even asked


@needs_bash
def test_a_stop_during_the_switch_says_that_it_may_still_run(tmp_path):
    # Ctrl-C while systemd-run waits: the switch runs on as its own unit, so the operator must not
    # run the script again before it has ended.
    run = _switch(tmp_path, HELD, extra="systemd-run() { kill -INT $$; }", stops=True)
    assert run.returncode != 0
    assert run.stderr.endswith(f"setup-caddy: stopped before it had finished. {SWITCH_ON_DISK}. The switch may still "
                               "run as webspec-caddy-cutover-20261006-120000: once it has ended (journalctl -u "
                               "webspec-caddy-cutover-20261006-120000), run this script again.\n")


@needs_bash
def test_the_switch_without_a_tunnel_to_stop(tmp_path):
    run = _switch(tmp_path, HELD, tunnel_active=False)
    assert run.returncode == 0, run.stderr
    assert "cloudflared" not in run.stdout and "command not found" not in run.stdout + run.stderr


@needs_bash
def test_the_switchs_program_carries_everything_it_runs(tmp_path):
    # systemd runs it with none of this script's functions or variables: a name it uses that
    # run_cutover does not pass would end the switch half-way, maybe with the tunnel stopped. The
    # tests above run some of its paths; this reads all of them.
    state = tmp_path / "state"
    state.mkdir()
    keep = r"""systemd-run() { cp -- "${!#}" "$STATE/program"; touch "$(dirname -- "${!#}")/log"; }
mktemp() { command mktemp -d "$STATE/cutover.XXXXXX"; }"""
    names = ("die", "warn", "unit_failed", "read_listeners", "owners", "unheld_listeners",
             "systemd_holds_listeners", "bind_failed", "cutover", "run_cutover")
    run = _bash("\n".join([f"STATE='{state}'", keep, *(_function(n) for n in names),
                           "SOCKET=s UNIT=u TUNNEL=t EXPECTED_LISTENERS=e CADDY_PORT=1 DIRECT_PORT=2",
                           "tunnel_stopped=0 STAMP=x CUTOVER_UNIT=y ON_DISK=d", "run_cutover"]))
    assert run.returncode == 0, run.stderr
    text = (state / "program").read_text()
    program = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    defined = set(re.findall(r"^(\w+) \(\) ?$", program, re.M))  # as declare -f prints them
    for name in set(re.findall(r"^(\w+)\(\) \{", TEXT, re.M)):
        if re.search(rf"(?<![\w-]){name}(?![\w-])", program):
            assert name in defined, f"the switch calls {name}, which run_cutover does not pass"
    declared = set(re.findall(r"^declare -[-\w]* (\w+)=", program, re.M))
    assigned = set(re.findall(r"(?<![\w$])([A-Za-z_]\w*)=", program))
    for name in set(re.findall(r"\$\{?([A-Za-z_]\w*)", program)):
        if name.isupper() or name == "tunnel_stopped":  # the globals; the rest are local
            assert name in declared | assigned, f"the switch reads ${name}, which run_cutover does not pass"
    assert program.rstrip().endswith("\ncutover") and "set -euo pipefail" in program


def test_the_operator_hears_of_the_switch_before_the_tunnel_stops():
    branch = CODE[CODE.index('if [ "$socket_changed" = 1 ] || ! systemd_holds_listeners; then'):]
    branch = branch[:branch.index("\nelif ")]
    assert branch.index("warn_foreign_tunnels") < branch.index("run_cutover")
    assert 'if [ "$foreign_warned" = 0 ]; then' in branch  # a switch on an active socket unit warns too
    run = _function("run_cutover")
    assert run.index("A session that") < run.index("${dir}/log, and starts the tunnel again") < run.index("systemd-run")


# ── Steps 4 and 5: a configuration that failed is never left in place ──

STAMP = "20261006-120000"

# Root's commands for steps 4 and 5, run as the test's user on a host under $ROOT. install keeps
# no owners, writes nothing outside $ROOT, and fails on a path that holds the text of
# $STATE/fail-install; mv, mkdir, rm and cp fail likewise for $STATE/fail-mv and the others, and
# sha256sum while $STATE/fail-sha256sum exists. Once mv has moved a path that holds the text of
# $STATE/interrupt-mv, the script is interrupted (Ctrl-C). systemd is state files under $STATE,
# as for the switch above: systemctl COMMAND fails while $STATE/fail-COMMAND exists, and once
# for $STATE/fail-COMMAND-once; it is interrupted for $STATE/interrupt-COMMAND, and ends the
# script without a word (exit 3) for $STATE/exit-COMMAND. A reload records the Caddyfile's first
# line. caddy validate (as_caddy) records what it checked, and which caddy was installed then; it
# fails while $STATE/fail-validate exists. py is webspec.caddy's own command line, on
# $ROOT/etc/caddy: it fails before apply writes if $STATE/fail-apply-early exists, once apply has
# written if $STATE/fail-apply does, and the script is interrupted there if $STATE/interrupt does.
INSTALL_STUBS = r"""
fails() { # fails COMMAND ARGS...: $STATE/fail-COMMAND exists, and ARGS hold its text
    local command=$1
    shift
    [ -e "$STATE/fail-$command" ] && [[ "$*" == *"$(cat "$STATE/fail-$command")"* ]]
}
mv() {
    if fails mv "$@"; then
        echo "mv: cannot move: Input/output error" >&2
        return 1
    fi
    command mv "$@" || return
    if [ -e "$STATE/interrupt-mv" ] && [[ "$*" == *"$(cat "$STATE/interrupt-mv")"* ]]; then kill -INT $$; fi
}
mkdir() {
    if fails mkdir "$@"; then echo "mkdir: cannot create directory: No space left on device" >&2; return 1; fi
    command mkdir "$@"
}
rm() {
    if fails rm "$@"; then echo "rm: cannot remove: Read-only file system" >&2; return 1; fi
    command rm "$@"
}
cp() {
    if fails cp "$@"; then echo "cp: error writing: No space left on device" >&2; return 1; fi
    command cp "$@"
}
sha256sum() {
    if [ -e "$STATE/fail-sha256sum" ]; then echo "sha256sum: -: Input/output error" >&2; return 1; fi
    command sha256sum "$@"
}
install() {
    local args=() writes=() arg
    while [ $# -gt 0 ]; do
        case "$1" in
            -o | -g) shift 2 ;;
            *) args+=("$1"); shift ;;
        esac
    done
    if [ "${args[0]}" = -d ]; then writes=("${args[@]:3}"); else writes=("${args[${#args[@]}-1]}"); fi
    for arg in "${writes[@]}"; do
        case "$arg" in
            "$ROOT"/*) ;;
            *) echo "install outside the test: $arg" >&2; return 1 ;;
        esac
        if [ -e "$STATE/fail-install" ] && [[ "$arg" == *"$(cat "$STATE/fail-install")"* ]]; then
            echo "install: error writing '$arg': No space left on device" >&2
            return 1
        fi
    done
    command install "${args[@]}" || return
    if [ -e "$STATE/interrupt-install" ] && [[ "${writes[*]}" == *"$(cat "$STATE/interrupt-install")"* ]]; then
        kill -INT $$
    fi
}
systemctl() {
    echo "$*" >> "$STATE/calls"
    if [ "$1" = reload ]; then head -n 1 -- "$CADDYFILE" >> "$STATE/reloaded" || true; fi
    if [ "$1" = reload ] && [ -e "$STATE/interrupt-second-reload" ] && [ "$(grep -c . "$STATE/reloaded")" = 2 ]; then
        kill -INT $$
    fi
    if [ "$1" = show ]; then [ -e "$STATE/mainpid" ] && cat "$STATE/mainpid"; return 0; fi
    if [ -e "$STATE/interrupt-$1" ]; then kill -INT $$; fi
    if [ -e "$STATE/exit-$1" ]; then exit 3; fi
    if [ -e "$STATE/fail-$1-once" ]; then
        command rm -f -- "$STATE/fail-$1-once"
        return 1
    fi
    if [ -e "$STATE/fail-$1" ]; then return 1; fi
    case "$1" in
        is-active) [ -e "$STATE/active-${3:-$2}" ] ;;
        is-enabled) return 1 ;;
    esac
}
stat() {
    if [ -e "$STATE/running-exe" ] && [[ "$*" == *"/proc/"*"/exe"* ]]; then cat "$STATE/running-exe"; return 0; fi
    command stat "$@"
}
ss() { if [ -e "$STATE/active-caddy-webspec.socket" ]; then printf '%s\n' "$HELD"; fi; }
journalctl() { echo "(journal of $*)"; }
as_caddy() {
    echo "$* (with $(cat "$CADDY_BIN") installed)" >> "$STATE/validated"
    if [ -e "$STATE/fail-validate" ]; then echo "Error: the test's caddy rejects it"; return 1; fi
}
py() {
    if [ -e "$STATE/fail-apply-early" ]; then
        echo "webspec.caddy: the test's failure, before apply writes" >&2
        return 1
    fi
    if [ -e "$STATE/term-during-apply" ]; then ( sleep 0.5; kill -TERM $$ ) & fi
    "$PYTHON" -I -c 'import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
etc = Path(sys.argv.pop(1))
from webspec import caddy
caddy.CADDYFILE, caddy.CADDY_CONF_DIR = etc / "Caddyfile", etc / "conf.d"
if (etc.parent.parent / "state" / "term-during-apply").exists(): time.sleep(1.5)
sys.exit(caddy.main(sys.argv[1:]))' "$GATEWAY" "$ROOT/etc/caddy" "$@" || return
    if [ -e "$STATE/fail-apply" ]; then echo "webspec.caddy: the test's failure, once apply has written" >&2; return 1; fi
    if [ -e "$STATE/interrupt" ]; then kill -INT $$; fi
}
"""


def _etc_caddy(root: Path) -> Path:
    """/etc/caddy under ``root``, as an earlier setup and an older webspec.caddy left it."""
    from webspec.caddy import IPV4_ONLY, generate_direct_site_block, generate_site_block, write_site_block

    etc = root / "etc" / "caddy"
    conf = etc / "conf.d"
    (conf / "old.caddy").mkdir(parents=True)
    # Rewritten (another domain, and the sockets of a kernel without IPv6), and removed: stale.
    write_site_block("op-auth", generate_site_block("op-auth", "old.example", guard=True, listen=IPV4_ONLY.gateway),
                     conf_dir=conf)
    write_site_block("gone", generate_site_block("gone", "old.example", listen=IPV4_ONLY.gateway), conf_dir=conf)
    write_site_block("web", generate_direct_site_block("web", "old.example", target_port=3000,
                                                       listen=IPV4_ONLY.direct), conf_dir=conf)
    # Moved aside: not written by webspec.caddy (the glob matches dot files too), writable by
    # others, a link, a FIFO, a directory.
    (conf / "hand.caddy").write_text("hand.example {\n\treverse_proxy 127.0.0.1:7002\n}\n")
    (conf / ".hidden.caddy").write_text("hidden.example {\n\trespond 200\n}\n")
    (conf / "..odd.caddy").write_text("odd.example {\n\trespond 200\n}\n")
    (conf / "shared.caddy").write_text((conf / "gone.caddy").read_text())
    (conf / "elsewhere.caddy").symlink_to("/nonexistent/elsewhere.caddy")
    os.mkfifo(conf / "pipe.caddy")
    (conf / "old.caddy" / "inside").write_text("x\n")
    # Left alone: what the import glob does not match.
    (conf / "README").write_text("Not matched by the import glob.\n")
    (conf / ".keep").write_text("")
    (etc / "Caddyfile").write_text(f"# Written by hand\n{{\n\tadmin off\n}}\nimport {conf}/*.caddy\n")
    _as_root_makes_them(root / "etc", etc, conf, conf / "old.caddy", conf / "old.caddy" / "inside", conf / "hand.caddy",
                        conf / ".hidden.caddy", conf / "..odd.caddy", conf / "README", conf / ".keep",
                        etc / "Caddyfile")
    (conf / "shared.caddy").chmod(0o664)
    return etc


def _files(*tops: Path) -> dict[str, tuple]:
    """Every entry at and below ``tops``: its bytes, link target or kind, its mode and its owners."""
    found = {}
    for top in tops:
        paths = [top]
        if top.is_dir() and not top.is_symlink():
            for directory, dirs, files in os.walk(top):
                paths += [Path(directory, name) for name in dirs + files]
        for path in paths:
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                what = os.readlink(path)
            elif stat.S_ISREG(st.st_mode):
                what = path.read_bytes()
            else:
                what = stat.S_IFMT(st.st_mode)  # a directory, a FIFO
            found[str(path.relative_to(top.parent))] = (what, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)
    return found


# The script's traps: cleanup at exit, and the signals that wait for the command that runs.
TRAPS = TEXT[TEXT.index("trap cleanup EXIT\n"):TEXT.index("\n\n", TEXT.index("trap cleanup EXIT\n"))]

# What steps 4 and 5 say Caddy needs once the new configuration stays (NOT_STARTED).
NOT_STARTED = ("Caddy was not reloaded or restarted, but that configuration needs this repository's units, so a "
               "start of Caddy before this script has finished (a crash, a reboot) may fail")


def _on_disk(root: Path, units: bool = False) -> str:
    """ON_DISK for the host under ``root``: the new configuration, and with ``units``, the units."""
    etc, unit_dir = root / "etc" / "caddy", root / "etc" / "systemd" / "system"
    text = (f"On disk: the new configuration ({etc}/Caddyfile, {etc}/conf.d/), which validated with "
            f"{root}/usr/local/bin/caddy")
    if units:
        text += f", and the units this repository ships ({unit_dir}/caddy-webspec.socket, caddy-webspec.service)"
    return text


def _inodes(etc: Path) -> dict[str, int]:
    """The inode of the Caddyfile and of each entry of conf.d but a directory, which is copied."""
    paths = [etc / "Caddyfile", *(etc / "conf.d").iterdir()]
    return {path.name: path.lstat().st_ino for path in paths if not stat.S_ISDIR(path.lstat().st_mode)}


def _install(root: Path, *, new_caddy: bool = True, set_up: bool = True, socket_unit: str | None = None,
             **state: str) -> subprocess.CompletedProcess:
    """Steps 4 and 5 of the script itself, with INSTALL_STUBS, on the host under ``root`` (_etc_caddy).

    ``set_up``: an earlier run installed the units, systemd holds the listeners and Caddy runs;
    otherwise the units are not there yet. ``new_caddy``: the caddy to install differs from the
    one installed. ``socket_unit``: the text of the socket unit installed now, an earlier setup's.
    Each of ``state`` names a file in $STATE (with - for _), and its text.
    """
    run_state = root / "state"
    run_state.mkdir()
    for name, text in state.items():
        (run_state / name.replace("_", "-")).write_text(text)
    caddy_bin = root / "usr" / "local" / "bin" / "caddy"
    unit_dir = root / "etc" / "systemd" / "system"
    for directory in (caddy_bin.parent, root / "work", root / "stage", unit_dir, root / "var" / "log"):
        directory.mkdir(parents=True, exist_ok=True)
    caddy_bin.write_bytes(b"old caddy\n")
    for built in (root / "work" / "caddy", root / "stage" / "caddy"):
        built.write_bytes(b"new caddy\n" if new_caddy else b"old caddy\n")
    (root / "config.json").write_text(json.dumps({"mcpServers": {"op-auth": {"command": "x", "guard": True},
                                                                  "mail-proton": {"command": "x"}}}))
    if set_up:
        for unit in ("caddy-webspec.socket", "caddy-webspec.service"):
            shutil.copy(UNIT_DIR / unit, unit_dir / unit)
            (run_state / f"active-{unit}").touch()
    if socket_unit is not None:
        (unit_dir / "caddy-webspec.socket").write_text(socket_unit)
    etc = root / "etc" / "caddy"
    # From "# ── 4. Install ──" to step 6: every /etc/caddy in it is the test's.
    steps = TEXT[TEXT.index("# ── 4. Install ──"):TEXT.index("# ── 6. Verify ──")].replace("/etc/caddy", str(etc))
    code = "\n".join([
        f"ROOT='{root}' STATE='{run_state}' PYTHON='{sys.executable}' GATEWAY='{GATEWAY_DIR}' HELD='{HELD}'",
        f"CADDYFILE='{etc}/Caddyfile' CONF_DIR='{etc}/conf.d' DISABLED_DIR='{etc}/conf.d.disabled'",
        f"LOG_DIR='{root}/var/log/caddy' CADDY_BIN='{caddy_bin}' WORK='{root}/work' STAGE='{root}/stage'",
        f"STAMP={STAMP} CONFIG='{root}/config.json' DOMAIN=new.example LISTENERS=dual SHRINK_FLAG=--allow-shrink",
        "GATEWAY_PORT=7002 CADDY_PORT=7001 DIRECT_PORT=7003",
        "UNIT=caddy-webspec.service SOCKET=caddy-webspec.socket TUNNEL=cloudflared.service",
        f"UNIT_DIR='{unit_dir}' UNIT_SRC_DIR='{UNIT_DIR}' IPV4_ONLY_DROPIN='{unit_dir}/caddy-webspec.socket.d/x.conf'",
        f"SHIPPED_UNITS='' EXPECTED_LISTENERS='{DUAL}' foreign_warned=1",
        "PREVIOUS='' put_back_on_exit=0 binary_changed=0 ON_DISK='' ON_STOP=''",
        INSTALL_STUBS, _function("die"), _function("warn"), _function("validate"), _function("cleanup"),
        TRAPS, steps,
        'ON_STOP=""'])  # as the script does once step 6 has verified the result
    return _bash(code)


@needs_bash
@pytest.mark.parametrize("failure", ["apply", "validate", "install", "mv", "interrupt"])
def test_a_configuration_that_fails_on_its_way_in_is_put_back_as_it_was(tmp_path, failure):
    # The installed copy does not validate although its staged copy did, apply fails half-way,
    # the new caddy cannot be installed or put in place, or the operator presses Ctrl-C: the files
    # Caddy loads at its next start are the ones it runs, byte for byte, never a configuration
    # that failed. They are the very files, not copies (hard links), so that what a copy can lose
    # (extended attributes, SELinux labels, other names of the file) stays theirs.
    etc = _etc_caddy(tmp_path)
    before, inodes = _files(etc / "Caddyfile", etc / "conf.d"), _inodes(etc)
    state = {"apply": {"fail_apply": ""}, "validate": {"fail_validate": ""},
             "install": {"fail_install": "caddy.new"}, "mv": {"fail_mv": "caddy.new"},
             "interrupt": {"interrupt": ""}}[failure]
    run = _install(tmp_path, **state)
    caddy_bin = tmp_path / "usr" / "local" / "bin" / "caddy"
    reason = {"apply": "could not write the configuration (see above)",
              "validate": "the installed configuration does not validate, although its staged copy did",
              "install": f"could not install {caddy_bin} (see above)",
              "mv": f"could not install {caddy_bin} (see above)",
              "interrupt": "stopped before the new configuration was installed"}[failure]
    assert run.returncode != 0 and "=== 5. Units" not in run.stdout
    assert "Caddy was left as it was" not in run.stderr
    assert (f"setup-caddy: {reason}. The previous {etc}/Caddyfile and {etc}/conf.d/ were put back as they were; "
            f"{caddy_bin} was not replaced, and Caddy was not reloaded or restarted.") in run.stderr
    assert _files(etc / "Caddyfile", etc / "conf.d") == before and _inodes(etc) == inodes
    assert caddy_bin.read_bytes() == b"old caddy\n" and not (caddy_bin.parent / "caddy.new").exists()
    # The files the new configuration moved aside are back in conf.d, and nowhere else.
    assert not (etc / "conf.d.disabled").exists() and not list(etc.glob("previous.*"))
    if failure == "validate":
        assert "Error: the test's caddy rejects it" in run.stderr
    if failure == "mv":  # install wrote caddy.new, which the mv could not put in place
        assert "mv: cannot move" in run.stderr


@needs_bash
def test_nothing_changes_until_the_previous_files_are_kept_aside(tmp_path):
    # put_back only renames: keep_previous makes what it needs before anything changes, so that a
    # full disk stops the script there, with the configuration that Caddy runs untouched.
    etc = _etc_caddy(tmp_path)
    before = _files(etc / "Caddyfile", etc / "conf.d")
    run = _install(tmp_path, fail_validate="", fail_mkdir="written/conf.d")
    assert run.returncode == 1 and "Site blocks:" not in run.stdout
    assert run.stderr.endswith(f"setup-caddy: could not keep {etc}/Caddyfile and {etc}/conf.d/ aside (see above); "
                               "the configuration was not changed\n")
    assert _files(etc / "Caddyfile", etc / "conf.d") == before
    assert not list(etc.glob("previous.*")) and not (etc / "conf.d.disabled").exists()


@needs_bash
@pytest.mark.parametrize("unmoved", ["previous", "new", "both", "all"])
def test_what_cannot_be_put_back_is_kept_and_named(tmp_path, unmoved):
    # A file of the previous configuration cannot be moved back, one this run wrote cannot be moved
    # out of the way (and none is moved onto it), or nothing moves (a read-only file system): the
    # rest moves all the same, the copy stays, and the message says what is where, from what moved.
    # It never says that the files are as they were, nor names a COPY/written that holds nothing.
    etc = _etc_caddy(tmp_path)
    before = _files(etc / "Caddyfile", etc / "conf.d")
    # "both": the new op-auth.caddy cannot be moved out of the way, while the previous one could
    # go back over it.
    run = _install(tmp_path, fail_validate="", fail_mv={"previous": "hand.caddy", "new": "mail-proton.caddy",
                                                        "both": f"conf.d/op-auth.caddy {etc}/previous.",
                                                        "all": str(etc)}[unmoved])
    copies = list(etc.glob("previous.*"))
    assert run.returncode == 1 and len(copies) == 1
    copy = copies[0]
    after = _files(etc / "Caddyfile", etc / "conf.d")
    kept = {k: v for k, v in _files(*(p for p in (copy / "Caddyfile", copy / "conf.d") if os.path.lexists(p))).items()
            if k != "conf.d"}
    said = "setup-caddy: the installed configuration does not validate, although its staged copy did. "
    assert "were put back as they were" not in run.stderr
    if unmoved == "previous":
        assert (f"{said}Not all of the previous configuration could be put back (see above): {copy} holds what is not "
                f"back in {etc}/Caddyfile and {etc}/conf.d/, and {copy}/written what this run moved out of the way. "
                "Caddy was not restarted: put the previous files back before it next starts.") in run.stderr
        assert after == {k: v for k, v in before.items() if k != "conf.d/hand.caddy"}
        assert kept == {"conf.d/hand.caddy": before["conf.d/hand.caddy"]}
        assert (copy / "written" / "Caddyfile").read_text().startswith("# Generated by webspec.caddy")
        assert (etc / "conf.d.disabled" / STAMP / "hand.caddy").exists()  # where apply moved it
    elif unmoved == "new":  # all that was there before is back, next to one file this run wrote
        assert (f"{said}Not all of what this run wrote could be moved out of the way (see above): what could not is "
                f"still in place, and {copy}/written holds the rest. Caddy was not restarted: remove those files "
                "before it next starts.") in run.stderr
        assert {k: v for k, v in after.items() if k != "conf.d/mail-proton.caddy"} == before
        assert "conf.d/mail-proton.caddy" in after and kept == {}
    elif unmoved == "both":  # the new op-auth.caddy stayed, so the previous one did not go over it
        assert (f"{said}Not all of the previous configuration could be put back (see above): {copy} holds what is not "
                f"back in {etc}/Caddyfile and {etc}/conf.d/, and {copy}/written what this run moved out of the way; "
                "what it could not move is still in place. Caddy was not restarted: put the previous files back "
                "before it next starts.") in run.stderr
        assert kept == {"conf.d/op-auth.caddy": before["conf.d/op-auth.caddy"]}
        assert "new.example" in (etc / "conf.d" / "op-auth.caddy").read_text()
        assert {k: v for k, v in after.items() if k != "conf.d/op-auth.caddy"} == \
            {k: v for k, v in before.items() if k != "conf.d/op-auth.caddy"}
    else:
        assert (f"{said}The previous configuration could not be put back (see above): what this run wrote is still "
                f"in place, and {copy} holds the previous configuration. Caddy was not restarted: put the previous "
                "files back before it next starts.") in run.stderr
        assert "/written" not in run.stderr
        assert not list((copy / "written" / "conf.d").iterdir()) and not (copy / "written" / "Caddyfile").exists()
        assert (etc / "Caddyfile").read_text().startswith("# Generated by webspec.caddy")
        assert kept == {k: v for k, v in before.items() if k != "conf.d"}


@needs_bash
@pytest.mark.parametrize("failure", ["validate", "apply"])
def test_a_first_install_that_fails_says_there_was_no_caddyfile(tmp_path, failure):
    # Nothing was there before, so no previous Caddyfile went back: the one this run wrote is gone,
    # or it wrote none, and conf.d is empty again.
    run = _install(tmp_path, set_up=False, **({"fail_validate": ""} if failure == "validate" else
                                              {"fail_apply_early": ""}))
    etc = tmp_path / "etc" / "caddy"
    caddy_bin = tmp_path / "usr" / "local" / "bin" / "caddy"
    if failure == "validate":
        said = (f"the installed configuration does not validate, although its staged copy did. There was no "
                f"{etc}/Caddyfile before: the one this run wrote was removed, and")
    else:
        said = (f"could not write the configuration (see above). There was no {etc}/Caddyfile before, nor is there "
                "one now, and")
    assert run.returncode == 1 and "were put back as they were" not in run.stderr
    assert (f"setup-caddy: {said} {etc}/conf.d/ was put back as it was; {caddy_bin} was not replaced, and Caddy was "
            "not reloaded or restarted.") in run.stderr
    assert not (etc / "Caddyfile").exists() and not list((etc / "conf.d").iterdir())
    assert not list(etc.glob("previous.*"))


@needs_bash
def test_a_stop_once_the_new_caddy_is_in_place_keeps_the_configuration_it_validated(tmp_path):
    # Ctrl-C right after the mv put the new caddy in place, before the script could note it (its
    # "Installed" line can wait on a full pipe): the new configuration validated with that caddy,
    # and the previous one never did, so the new one stays, and the message says what is on disk.
    etc = _etc_caddy(tmp_path)
    caddy_bin = tmp_path / "usr" / "local" / "bin" / "caddy"
    run = _install(tmp_path, interrupt_mv="caddy.new")
    assert run.returncode != 0 and "=== 5. Units" not in run.stdout and "put back" not in run.stderr
    assert run.stderr.endswith(f"setup-caddy: stopped once {caddy_bin} had been replaced. {_on_disk(tmp_path)}. "
                               f"{NOT_STARTED}. Run this script again.\n")
    assert caddy_bin.read_bytes() == b"new caddy\n"
    caddyfile = (etc / "Caddyfile").read_text()
    assert caddyfile.startswith("# Generated by webspec.caddy") and "new.example" in caddyfile
    assert not list(etc.glob("previous.*"))


@needs_bash
@pytest.mark.parametrize("stop", ["signal in daemon-reload", "signal in restart", "exit in restart"])
def test_a_stop_in_step_5_says_what_is_on_disk(tmp_path, stop):
    # Ctrl-C (or a hangup) while systemd works, or a command that ends the script without a word:
    # the new configuration stays, and the operator hears what is on disk.
    etc = _etc_caddy(tmp_path)
    how, command = stop.split(" in ")
    run = _install(tmp_path, **{f"{'interrupt' if how == 'signal' else 'exit'}_{command.replace('-', '_')}": ""})
    assert run.returncode != 0
    said = _on_disk(tmp_path, units=True) if command == "restart" else f"{_on_disk(tmp_path)}. {NOT_STARTED}"
    assert run.stderr.endswith(f"setup-caddy: stopped before it had finished. {said}. Run this script again.\n")
    assert (etc / "Caddyfile").read_text().startswith("# Generated by webspec.caddy")
    assert not list(etc.glob("previous.*"))


@needs_bash
def test_a_stop_while_the_previous_files_go_back_keeps_them_and_says_where(tmp_path):
    # Ctrl-C half-way through the put-back: the copy stays, and the message says where things are.
    etc = _etc_caddy(tmp_path)
    run = _install(tmp_path, fail_validate="", interrupt_mv="README")
    copies = list(etc.glob("previous.*"))
    assert run.returncode != 0 and len(copies) == 1
    copy = copies[0]
    assert run.stderr.endswith(f"setup-caddy: stopped before it had finished. The previous configuration was being "
                               f"put back: {copy} holds what is not back in {etc}/Caddyfile and {etc}/conf.d/, and "
                               f"{copy}/written what this run moved out of the way. Put the previous files back "
                               "before Caddy next starts.\n")
    assert (copy / "written" / "conf.d" / "README").exists() and (copy / "conf.d" / "hand.caddy").exists()


@needs_bash
def test_the_new_caddy_validates_the_installed_configuration_before_it_is_installed(tmp_path):
    etc = _etc_caddy(tmp_path)
    run = _install(tmp_path)
    assert run.returncode == 0, run.stderr
    assert "Restarted caddy-webspec.service; caddy-webspec.socket kept the ports bound" in run.stdout
    # Validated where it is installed, with the new caddy, while the one in place was the old one.
    assert (tmp_path / "state" / "validated").read_text().splitlines() == [
        f"{tmp_path}/stage/caddy validate --config {etc}/Caddyfile (with old caddy installed)"]
    assert (tmp_path / "usr" / "local" / "bin" / "caddy").read_bytes() == b"new caddy\n"
    caddyfile = (etc / "Caddyfile").read_text()
    assert caddyfile.startswith("# Generated by webspec.caddy") and "new.example" in caddyfile
    assert sorted(p.name for p in (etc / "conf.d").iterdir()) == [
        ".keep", "README", "mail-proton.caddy", "op-auth.caddy", "web.caddy"]
    assert sorted(p.name for p in (etc / "conf.d.disabled" / STAMP).iterdir()) == [
        "..odd.caddy", ".hidden.caddy", "elsewhere.caddy", "hand.caddy", "old.caddy", "pipe.caddy", "shared.caddy"]
    assert not list(etc.glob("previous.*"))  # the copy goes once the script ends
    assert "stopped before it had finished" not in run.stderr


@needs_bash
@pytest.mark.parametrize("again", ["reloads", "fails"])
def test_a_reload_that_fails_puts_the_previous_configuration_back(tmp_path, again):
    # Only the configuration changed, so Caddy reloads it. When that fails, the previous files go
    # back, and Caddy reloads them: had it loaded the new configuration all the same (a reload that
    # timed out), it would otherwise run one that is no longer on disk. The message says what came
    # of the second reload, and claims nothing that the script cannot know.
    etc = _etc_caddy(tmp_path)
    before = _files(etc / "Caddyfile", etc / "conf.d")
    caddy_bin = tmp_path / "usr" / "local" / "bin" / "caddy"
    run = _install(tmp_path, new_caddy=False, **({"fail_reload_once": ""} if again == "reloads" else
                                                 {"fail_reload": ""}))
    assert run.returncode == 1
    assert "(journal of -u caddy-webspec.socket -u caddy-webspec.service -n 30 --no-pager)" in run.stderr
    assert (f"setup-caddy: caddy-webspec.service did not reload (see above). The previous {etc}/Caddyfile and "
            f"{etc}/conf.d/ were put back as they were; {caddy_bin} and the units were not changed, and Caddy was "
            "not restarted.") in run.stderr
    if again == "reloads":
        assert run.stderr.endswith("setup-caddy: reloaded caddy-webspec.service with the files put back: Caddy runs "
                                   "the previous configuration. Fix what the journal above shows, then run this "
                                   "script again.\n")
    else:
        # The journal again, for the second failure: "see above" names it.
        assert "(journal of -u caddy-webspec.service -n 10 --no-pager)\nsetup-caddy: caddy-webspec.service did " \
               "not reload the files put back either" in run.stderr
        assert run.stderr.endswith("setup-caddy: caddy-webspec.service did not reload the files put back either (see "
                                   "above). If the first reload timed out, Caddy may still run the new configuration: "
                                   "once the journal above shows why, have it load the files on disk (sudo systemctl "
                                   "reload caddy-webspec), then run this script again.\n")
    assert "leaves it with the configuration it ran" not in run.stderr
    # The first reload read the new Caddyfile; the second, the one put back.
    reloaded = (tmp_path / "state" / "reloaded").read_text().splitlines()
    assert len(reloaded) == 2 and reloaded[0].startswith("# Generated by webspec.caddy")
    assert reloaded[1] == "# Written by hand"
    assert _files(etc / "Caddyfile", etc / "conf.d") == before
    assert not (etc / "conf.d.disabled").exists() and not list(etc.glob("previous.*"))


# An earlier setup's socket unit, which this repository did not ship.
EARLIER_SOCKET = "[Socket]\nListenStream=127.0.0.1:7001\n"


@needs_bash
@pytest.mark.parametrize("new_caddy", [False, True])
@pytest.mark.parametrize("failure", ["cp", "sha256sum"])
def test_a_replaced_unit_that_cannot_be_kept_aside(tmp_path, failure, new_caddy):
    # An earlier setup's unit cannot be read, or copied aside, before it is replaced; no unit was
    # written yet. With the caddy that ran the previous configuration still in place, nothing else
    # has changed: that configuration goes back, and Caddy's next start runs what it runs now. A
    # new caddy never validated it: then the new configuration stays, and the message says that
    # it needs this repository's units.
    etc = _etc_caddy(tmp_path)
    before = _files(etc / "Caddyfile", etc / "conf.d")
    unit = tmp_path / "etc" / "systemd" / "system" / "caddy-webspec.socket"
    caddy_bin = tmp_path / "usr" / "local" / "bin" / "caddy"
    run = _install(tmp_path, new_caddy=new_caddy, socket_unit=EARLIER_SOCKET,
                   **{f"fail_{failure}": "caddy-webspec.socket"})
    reason = f"could not read {unit} (see above)" if failure == "sha256sum" else \
        f"could not copy {unit} aside (see above)"
    assert run.returncode == 1 and "stopped before it had finished" not in run.stderr
    if new_caddy:
        assert (f"setup-caddy: {reason}. {_on_disk(tmp_path)}. {NOT_STARTED}. Fix the cause, then run this script "
                "again.") in run.stderr
        assert (etc / "Caddyfile").read_text().startswith("# Generated by webspec.caddy")
        assert caddy_bin.read_bytes() == b"new caddy\n"
    else:
        assert (f"setup-caddy: {reason}. The previous {etc}/Caddyfile and {etc}/conf.d/ were put back as they were; "
                f"{caddy_bin} and the units were not changed, and Caddy was not reloaded or restarted.") in run.stderr
        assert _files(etc / "Caddyfile", etc / "conf.d") == before
        assert caddy_bin.read_bytes() == b"old caddy\n"
    assert unit.read_text() == EARLIER_SOCKET and not list(unit.parent.glob("*.bak.*"))
    assert not (tmp_path / "state" / "calls").exists() and not list(etc.glob("previous.*"))


@needs_bash
@pytest.mark.parametrize("failure", ["unit", "drop-in", "daemon-reload", "restart"])
def test_a_failure_once_the_units_change_says_what_is_on_disk(tmp_path, failure):
    # The units, or Caddy's restart with them, may not suit the previous configuration: the new
    # one, which validated, stays, and the message says so, that it needs these units, and what to
    # do. It is the only message: cleanup adds none.
    etc = _etc_caddy(tmp_path)
    caddy_bin = tmp_path / "usr" / "local" / "bin" / "caddy"
    unit_dir = tmp_path / "etc" / "systemd" / "system"
    not_started = f"{NOT_STARTED}. Fix the cause, then run this script again."
    if failure == "unit":  # a first install, on a full disk
        run = _install(tmp_path, set_up=False, fail_install="caddy-webspec.socket")
        message = (f"could not install {unit_dir}/caddy-webspec.socket, which may now be missing or incomplete "
                   f"(see above). {_on_disk(tmp_path)}. {not_started}")
    elif failure == "drop-in":  # an earlier version's IPv4-only drop-in, on a read-only /etc
        dropin = unit_dir / "caddy-webspec.socket.d" / "x.conf"  # IPV4_ONLY_DROPIN in _install
        dropin.parent.mkdir(parents=True)
        dropin.write_text("[Socket]\nListenStream=\nListenStream=127.0.0.1:7001\n")
        run = _install(tmp_path, fail_rm="x.conf")
        message = f"could not remove {dropin} (see above). {_on_disk(tmp_path)}. {not_started}"
    elif failure == "daemon-reload":
        run = _install(tmp_path, set_up=False, fail_daemon_reload="")
        message = f"systemctl daemon-reload failed (see above). {_on_disk(tmp_path)}. {not_started}"
    else:
        run = _install(tmp_path, fail_restart="")
        message = (f"caddy-webspec.service did not restart. {_on_disk(tmp_path, units=True)}. Fix what the journal "
                   "above shows, then run this script again.")
    assert run.returncode == 1
    assert run.stderr.endswith(f"setup-caddy: {message}\n")
    caddyfile = (etc / "Caddyfile").read_text()
    assert caddyfile.startswith("# Generated by webspec.caddy") and "new.example" in caddyfile
    assert caddy_bin.read_bytes() == b"new caddy\n" and not list(etc.glob("previous.*"))


# ── F9: plan first, and compare what each host answers ──


def test_the_plan_comes_before_any_change_and_its_refusal_changes_nothing():
    plan = TEXT.index("py plan --config")
    assert plan < TEXT.index("build_caddy \"${WORK}/caddy\"") < TEXT.index("py apply")
    assert '3) die "nothing was changed. To go ahead anyway, and stop serving what is named above, run this ' \
           'again with ALLOW_SHRINK=1.' in TEXT
    probe_before = TEXT.index('was="$(probe "$host" "$before_port")"')
    assert plan < probe_before < TEXT.index("py apply")


def test_a_development_host_must_name_its_config():
    m = re.search(r'^if \[ -n "\$\{WEBSPEC_CONFIG:-\}" \]; then\n(.*?)^fi$', TEXT, re.S | re.M)
    assert m and "die " in m.group(1) and "WEBSPEC_CONFIG is not set" in m.group(1)
    assert ".claude.json" not in m.group(1).split("die ")[0]  # no fallback to a file a user can write


@needs_bash
@pytest.mark.parametrize("code,served", [("200", True), ("401", True), ("502", True), ("421", False),
                                         ("000", False), ("-", False)])
def test_what_counts_as_served(code, served):
    assert (_bash(_function("served") + f"\nserved {code}").returncode == 0) is served


@needs_bash
@pytest.mark.parametrize("now,ok,note", [
    ({"a.example.com": "200", "b.example.com": "200"}, True, ""),
    ({"a.example.com": "502", "b.example.com": "200"}, False, "a.example.com                            200 -> 502 "
                                                              "FAILS NOW: check what it proxies to"),
    ({"a.example.com": "421", "b.example.com": "200"}, False, "a.example.com                            200 -> 421 "
                                                              "NOT SERVED"),
    ({"a.example.com": "200", "b.example.com": "000"}, False, "NOT SERVED"),
])
def test_the_verification_compares_each_host_with_before(tmp_path, now, ok, note):
    # The F9 comparison as step 6 runs it: before -> now, for the hosts Caddy served before.
    served = tmp_path / "served"
    served.write_text("a.example.com\t7001\tkept\t200\nb.example.com\t7003\tmoved\t200\n"
                      "gone.example.com\t7001\tdropped\t200\nfailing.example.com\t7001\tkept\t502\n"
                      "web.example.com\t7001\tleft-direct\t200\n")
    codes = {**now, "gone.example.com": "421", "failing.example.com": "502", "web.example.com": "401"}
    probe = "probe() { case \"$1\" in " + " ".join(f'{h}) echo {c} ;;' for h, c in codes.items()) + " esac; }"
    run = _bash("\n".join([probe, _function("served"), _function("verify_hosts"),
                           f"if verify_hosts '{served}'; then echo PASSED; else echo FAILED; fi"]))
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert lines[-1] == ("PASSED" if ok else "FAILED")
    assert note in run.stdout
    assert "    gone.example.com                         200 -> 421 no longer served, as ALLOW_SHRINK=1 allowed" in lines
    assert "    failing.example.com                      502 -> 502" in lines  # failing before: not its doing
    # P10: a direct site that a gateway service replaced is on the gateway's listener, and guarded.
    assert "    web.example.com                          200 -> 401 now through the gateway (127.0.0.1:7001), " \
           "no longer a direct site" in lines
    if ok:
        assert "    b.example.com                            200 -> 200 now on the direct listener (127.0.0.1:7003)" in lines


# ── F27, F31, F36: the advice at the end ──


def test_replacing_an_earlier_setup_warns_about_the_journal():
    detect = TEXT[TEXT.index("earlier_setup=0"):TEXT.index("# ── 1. Caddy")]
    assert "grep -qx 'User=caddy'" in detect and '"# Generated by webspec.caddy"*' in detect
    warning = TEXT[TEXT.index('if [ "$earlier_setup" = 1 ]; then'):]
    for text in ("journalctl -u ${UNIT}", "query strings", "X-WebSpec-Guard", "X-UFO-Clearance",
                 "sudo journalctl --rotate && sudo journalctl --vacuum-time=1s", "adm and systemd-journal"):
        assert text in warning, text


def test_the_domain_advice_restarts_the_gateway_and_needs_no_etc_webspec():
    settings = TEXT[TEXT.index("if [ -f \"$PRODUCTION_CONFIG\" ]; then\n    GATEWAY_SETTINGS"):]
    assert 'RESTART_GATEWAY="sudo systemctl restart webspec-gateway"' in settings
    assert "systemctl --user restart webspec-gateway" in settings
    advice = TEXT[TEXT.index("No public domain: Caddy serves"):TEXT.index("DP-3: the agent")]
    assert "${GATEWAY_SETTINGS}" in advice and "${RESTART_GATEWAY}" in advice
    assert "/etc/webspec" not in advice  # F36: a development host is never told to create it
    hint = TEXT[TEXT.index("To change the public domain, give it once"):]
    assert "${RESTART_GATEWAY}" in hint


def test_the_socket_unit_lists_both_families_ipv4_first():
    # On a kernel without IPv6 systemd ignores the [::1] lines, so the IPv4 sockets are fds 3 and 4
    # in both layouts (webspec.caddy.Listeners).
    unit = [line.split("=", 1)[1] for line in (UNIT_DIR / "caddy-webspec.socket").read_text().splitlines()
            if line.startswith("ListenStream=")]
    assert unit == ["127.0.0.1:7001", "127.0.0.1:7003", "[::1]:7001", "[::1]:7003"]


def _drop_in_step() -> str:
    """Step 5's handling of the IPv4-only drop-in, from its test to the matching fi."""
    m = re.search(r'^if \[ -e "\$IPV4_ONLY_DROPIN" \].*?^fi$', TEXT, re.S | re.M)
    assert m, "setup-caddy.sh no longer removes the IPv4-only drop-in"
    return m.group(0)


def test_the_script_never_drops_the_ipv6_listeners():
    # P2, the other way round: a drop-in that kept the IPv4 lines alone outlived the kernel it was
    # written for. Booted with IPv6, nothing held [::1]:7001 and [::1]:7003, and any local user
    # could listen there and receive what local clients sent to Caddy (DP-9).
    assert "ListenStream" not in CODE
    assert "ipv4-only.conf" not in CODE.replace("IPV4_ONLY_DROPIN=${UNIT_DIR}/${SOCKET}.d/10-ipv4-only.conf", "")
    # Removed with the units installed, before systemd reloads them and the switch restarts them.
    step = TEXT.index(_drop_in_step())
    assert TEXT.index('install_if_changed "${UNIT_SRC_DIR}/${SOCKET}"') < step < TEXT.index("systemctl daemon-reload")


@needs_bash
@pytest.mark.parametrize("leftover", ["file", "symlink", "none"])
def test_an_earlier_setups_ipv4_only_drop_in_is_removed_and_the_sockets_switched(tmp_path, leftover):
    dropin = tmp_path / "caddy-webspec.socket.d" / "10-ipv4-only.conf"
    if leftover != "none":
        dropin.parent.mkdir()
        if leftover == "file":
            dropin.write_text("[Socket]\nListenStream=\nListenStream=127.0.0.1:7001\nListenStream=127.0.0.1:7003\n")
        else:
            dropin.symlink_to(tmp_path / "gone")
    run = _bash("\n".join([f"IPV4_ONLY_DROPIN='{dropin}'", "socket_changed=0", _drop_in_step(),
                           'echo "socket_changed=$socket_changed"']))
    assert run.returncode == 0, run.stderr
    assert not dropin.exists() and not dropin.is_symlink() and not dropin.parent.exists()
    if leftover == "none":
        assert run.stdout == "socket_changed=0\n"
    else:  # the switch restarts the socket unit, which binds [::1] wherever the kernel has IPv6
        assert run.stdout.splitlines() == [
            f"Removed {dropin}, which an earlier setup wrote: systemd holds [::1] wherever the kernel has IPv6",
            "socket_changed=1"]


@needs_bash
def test_a_drop_in_directory_with_other_files_is_kept(tmp_path):
    dropin = tmp_path / "caddy-webspec.socket.d" / "10-ipv4-only.conf"
    dropin.parent.mkdir()
    dropin.write_text("[Socket]\nListenStream=\n")
    (dropin.parent / "20-mine.conf").write_text("[Socket]\nBacklog=128\n")
    run = _bash("\n".join([f"IPV4_ONLY_DROPIN='{dropin}'", "socket_changed=0", _drop_in_step()]))
    assert run.returncode == 0, run.stderr
    assert not dropin.exists() and (dropin.parent / "20-mine.conf").exists()


# ── P2: IPv6 turned on or off at boot after setup ──

_NO_IPV6 = ("caddy-webspec: /etc/caddy/Caddyfile binds the [::1] listeners, but this kernel has no IPv6; "
            "run gateway/tools/setup-caddy.sh again")


def _exec_start_pre() -> list[str]:
    """caddy-webspec.service's ExecStartPre=, split as systemd does: continued lines joined, then unquoted."""
    text = re.sub(r"\\\n", " ", (UNIT_DIR / "caddy-webspec.service").read_text())
    lines = [line for line in text.splitlines() if line.startswith("ExecStartPre=")]
    assert len(lines) == 1
    value = lines[0].split("=", 1)[1]
    assert "$" not in value and "%" not in value  # systemd would expand them
    return shlex.split(value)


def _check(caddyfile: Path, if_inet6: Path) -> subprocess.CompletedProcess:
    """Run the unit's check with these two paths in place of the real ones (the message keeps its own)."""
    argv = _exec_start_pre()
    assert argv[:2] == ["/bin/sh", "-c"] and len(argv) == 3
    script = argv[2]
    assert script.count("/etc/caddy/Caddyfile") == 2 and script.count("/proc/self/net/if_inet6") == 1
    script = script.replace("/proc/self/net/if_inet6", str(if_inet6)).replace("/etc/caddy/Caddyfile", str(caddyfile), 1)
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, timeout=30)


def _prose(text: str) -> str:
    """The comment lines of a unit or script, as one line of text."""
    return " ".join(line.lstrip("#").strip() for line in text.splitlines() if line.startswith("#"))


@pytest.mark.parametrize("layout,ipv6,refused", [("dual", True, False), ("dual", False, True),
                                                 ("ipv4", False, False), ("ipv4", True, False)])
def test_caddy_is_not_started_with_ipv6_sockets_on_a_kernel_without_ipv6(tmp_path, layout, ipv6, refused):
    # On a kernel booted with ipv6.disable=1, systemd ignores the socket unit's [::1] lines, and a
    # Caddyfile that binds fd/5 and fd/6 would fail at every restart with "listening on fd/6:
    # socket operation on non-socket". The unit's ExecStartPre says what to do instead.
    from webspec.caddy import LAYOUTS, generate_global_caddyfile

    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(generate_global_caddyfile(conf_dir=tmp_path / "conf.d", listen=LAYOUTS[layout]))
    if_inet6 = tmp_path / "net" / "if_inet6"  # /proc/self/net/if_inet6: there exactly when the kernel has IPv6
    if ipv6:
        if_inet6.parent.mkdir()
        if_inet6.write_text("00000000000000000000000000000001 01 80 10 80       lo\n")
    run = _check(caddyfile, if_inet6)
    assert (run.returncode, run.stdout, run.stderr) == ((1, "", _NO_IPV6 + "\n") if refused else (0, "", ""))


def test_the_check_reads_binds_only_and_leaves_a_missing_caddyfile_to_caddy(tmp_path):
    no_ipv6 = tmp_path / "no-net" / "if_inet6"
    commented = tmp_path / "Caddyfile"
    commented.write_text("# bind fd/3 fd/5, in a comment\n{\n\tdefault_bind fd/3\n}\n")
    assert _check(commented, no_ipv6).returncode == 0
    assert _check(tmp_path / "missing", no_ipv6).returncode == 0  # Caddy says what is missing


def test_the_check_reads_the_live_configuration_where_procsubset_leaves_proc_net():
    # ProcSubset=pid hides /proc/net (a symbolic link to self/net) but not /proc/self/net.
    unit = (UNIT_DIR / "caddy-webspec.service").read_text()
    assert "\nProcSubset=pid\n" in unit and "\nUser=caddy\n" in unit
    from webspec.caddy import CADDYFILE

    script = _exec_start_pre()[2]
    assert f"grep -q \"^[^#]*bind .*fd/[56]\" {CADDYFILE} " in script and "[ ! -e /proc/self/net/if_inet6 ]" in script
    assert unit.index("ExecStartPre=") < unit.index("ExecStart=/usr/local/bin/caddy run")


def test_the_units_and_the_script_say_to_run_setup_again_after_an_ipv6_change():
    header = _prose(TEXT[:TEXT.index("set -euo pipefail")])
    assert "Run the script again after turning IPv6 on or off at boot." in header
    assert "Turning IPv6 off with sysctl (net.ipv6.conf.*.disable_ipv6) needs no re-run" in header
    # Both ways round: a kernel without IPv6 cannot start a configuration that binds [::1], and
    # one with IPv6 has systemd hold [::1], which a configuration written without IPv6 leaves
    # unanswered. Never unheld: the drop-in that let go of them is gone (P2).
    assert "Booted with IPv6 again, systemd holds [::1]:7001 and [::1]:7003" in header
    assert "any local user could listen there; the script removes it." in header
    socket = _prose((UNIT_DIR / "caddy-webspec.socket").read_text())
    assert "Never drop the [::1] lines in a drop-in: on a kernel with IPv6, nothing would hold those ports" in socket
    assert "10-ipv4-only.conf" not in socket
    service = _prose((UNIT_DIR / "caddy-webspec.service").read_text())
    assert "booted with IPv6 again, systemd holds [::1]:7001 and [::1]:7003" in service
    for unit in ("caddy-webspec.socket", "caddy-webspec.service"):
        text = (UNIT_DIR / unit).read_text()
        assert "Run setup-caddy.sh again after turning IPv6 on or off at boot (ipv6.disable=1)" in _prose(text), unit
        assert re.search(r"^Documentation=https://caddyserver\.com/docs/ "
                         r"https://i-m-a-g-i-n-e\.github\.io/WebSpec/guide/deploy/$", text, re.M), unit
        assert "gimme.tools" not in text, unit


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not available")
def test_shellcheck_is_clean():
    result = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


@needs_bash
def test_a_signal_waits_for_the_command_that_runs(tmp_path):
    # SIGTERM to the script while apply writes: cleanup runs only once apply has finished, so the
    # previous files go back over everything apply wrote, never under a configuration still being
    # written. Untrapped, bash would run cleanup at once, and apply would write over the files put
    # back.
    etc = _etc_caddy(tmp_path)
    before = _files(etc / "Caddyfile", etc / "conf.d")
    caddy_bin = tmp_path / "usr" / "local" / "bin" / "caddy"
    run = _install(tmp_path, term_during_apply="")
    time.sleep(2)  # an apply that outlived the script would have written by now
    assert run.returncode == 143
    assert (f"setup-caddy: stopped before the new configuration was installed. The previous {etc}/Caddyfile and "
            f"{etc}/conf.d/ were put back as they were; {caddy_bin} was not replaced, and Caddy was not reloaded or "
            "restarted.") in run.stderr
    assert _files(etc / "Caddyfile", etc / "conf.d") == before
    assert caddy_bin.read_bytes() == b"old caddy\n"


@needs_bash
def test_a_stop_while_the_new_caddy_is_written_puts_the_previous_configuration_back(tmp_path):
    # Ctrl-C once install has written caddy.new, before the mv: the old caddy is still the one in
    # place, so the previous configuration goes back, and caddy.new does not stay behind.
    etc = _etc_caddy(tmp_path)
    before = _files(etc / "Caddyfile", etc / "conf.d")
    caddy_bin = tmp_path / "usr" / "local" / "bin" / "caddy"
    run = _install(tmp_path, interrupt_install="caddy.new")
    assert run.returncode != 0 and "=== 5. Units" not in run.stdout
    assert (f"setup-caddy: stopped before the new configuration was installed. The previous {etc}/Caddyfile and "
            f"{etc}/conf.d/ were put back as they were; {caddy_bin} was not replaced, and Caddy was not reloaded or "
            "restarted.") in run.stderr
    assert _files(etc / "Caddyfile", etc / "conf.d") == before
    assert caddy_bin.read_bytes() == b"old caddy\n" and not (caddy_bin.parent / "caddy.new").exists()


@needs_bash
def test_a_stop_during_the_second_reload_says_caddy_may_run_the_new_configuration(tmp_path):
    # The first reload failed, the previous files went back, and Ctrl-C comes while Caddy reloads
    # them: the files are right, but which configuration Caddy runs is not known.
    _etc_caddy(tmp_path)
    run = _install(tmp_path, new_caddy=False, fail_reload_once="", interrupt_second_reload="")
    assert run.returncode != 0
    assert run.stderr.endswith("setup-caddy: stopped before it had finished. The previous configuration is back on "
                               "disk, but Caddy may still run the new one: have it load the files on disk (sudo "
                               "systemctl reload caddy-webspec).\n")


@needs_bash
def test_a_caddy_that_runs_another_binary_is_restarted_not_reloaded(tmp_path):
    # A run that stopped once the new caddy was in place left the old one running; this run builds
    # the same caddy, changes no unit, and finds the listeners held. Only a restart puts the
    # installed caddy in charge of the configuration that validated with it.
    _etc_caddy(tmp_path)
    run = _install(tmp_path, new_caddy=False, mainpid="4242", running_exe="1:2")
    assert run.returncode == 0, run.stderr
    assert "Restarted caddy-webspec.service; caddy-webspec.socket kept the ports bound" in run.stdout
    calls = (tmp_path / "state" / "calls").read_text().splitlines()
    assert "show -p MainPID --value caddy-webspec.service" in calls
    assert "restart caddy-webspec.service" in calls and not any(c.startswith("reload") for c in calls)
