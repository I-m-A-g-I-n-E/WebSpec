"""Tests for webspec-ctl's way of changing the live Caddy configuration (webspec.caddy).

- the trusted caddy: never a bare `caddy` looked up on PATH, never one another user can replace
  when running as root, and always with a clean environment;
- caddy_status: only a host that setup-caddy.sh migrated is changed; macOS has no Caddy;
- apply_site_changes: staged, validated, written, reloaded, and every file put back when Caddy
  does not load the change;
- the public domain recorded in the generated Caddyfile, reused when none is given.

Caddy is a stand-in executable (FakeCaddy): it records its calls and a copy of what it was asked
to validate, and fails a command on request. tests/test_caddy.py runs the real one when
WEBSPEC_TEST_CADDY is set.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from webspec import caddy, config_writer
from webspec.caddy import (
    ABSENT,
    DUAL,
    MANAGED,
    REFUSED,
    CaddyError,
    DomainError,
    Site,
    apply_site_changes,
    caddy_status,
    generate_direct_site_block,
    generate_global_caddyfile,
    generate_site_block,
    read_env_file_value,
    read_site,
    recorded_domain,
    reload_caddy,
    resolve_domain,
    trusted_caddy,
    unsafe_component,
    write_site_block,
)

_SCRIPT = """#!/bin/sh
# A stand-in caddy: records the call and its environment, keeps a copy of what it validates,
# and fails a command when a file fail-<command> exists next to it.
d=$(dirname "$0")
printf '%s\\n' "$*" >> "$d/calls.log"
env | sort > "$d/env-$1"
if [ "$1" = validate ]; then
    rm -rf "$d/last-stage"
    cp -R "$(dirname "$3")" "$d/last-stage"
fi
if [ -f "$d/fail-$1" ]; then
    cat "$d/fail-$1" >&2
    exit 1
fi
exit 0
"""


class FakeCaddy:
    """A caddy executable that records what it is asked to do."""

    def __init__(self, root: Path):
        self.dir = root / "fake-caddy"
        self.dir.mkdir()
        self.dir.chmod(0o755)  # whatever the umask
        self.path = self.dir / "caddy"
        self.path.write_text(_SCRIPT)
        self.path.chmod(0o755)

    def fail(self, command: str, stderr: str) -> None:
        (self.dir / f"fail-{command}").write_text(stderr)

    def calls(self) -> list[str]:
        log = self.dir / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def env(self, command: str) -> dict[str, str]:
        lines = (self.dir / f"env-{command}").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)

    @property
    def stage(self) -> Path:
        return self.dir / "last-stage"


@dataclass
class CaddyHost:
    """A Linux host that setup-caddy.sh migrated, under tmp_path."""

    root: Path
    caddyfile: Path
    conf: Path
    caddy: FakeCaddy

    def block(self, name: str) -> Path:
        return self.conf / f"{name}.caddy"

    def snapshot(self) -> dict[str, bytes]:
        files = {p.name: p.read_bytes() for p in self.conf.iterdir()}
        files["Caddyfile"] = self.caddyfile.read_bytes()
        return files


def make_caddy_host(tmp_path: Path, monkeypatch, domain: str | None = "") -> CaddyHost:
    """Point webspec.caddy at a migrated host in tmp_path; ``domain`` None writes no Caddyfile.

    The files stand for root's, which setup-caddy.sh writes under its own umask 022: their modes
    are set here, not left to the umask of whoever runs the suite (002 for a user with a private
    group, under which webspec.caddy rightly distrusts a group-writable Caddyfile).
    """
    etc = tmp_path / "etc-caddy"
    conf = etc / "conf.d"
    conf.mkdir(parents=True)
    for directory in (etc, conf):
        directory.chmod(0o755)
    caddyfile = etc / "Caddyfile"
    if domain is not None:
        caddyfile.write_text(generate_global_caddyfile(conf_dir=conf, domain=domain, listen=DUAL))
        caddyfile.chmod(0o644)
    monkeypatch.setattr(caddy, "CADDYFILE", caddyfile)
    monkeypatch.setattr(caddy, "CADDY_CONF_DIR", conf)
    monkeypatch.setattr(caddy, "_on_macos", lambda: False)
    monkeypatch.setattr(caddy, "ipv6_supported", lambda: True)
    # caddy-webspec.socket as setup-caddy.sh installs it with IPv6, as systemd reports it, whatever
    # this machine has installed; tests of other layouts patch this.
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: DUAL.addresses)
    monkeypatch.setattr(caddy, "IPV4_ONLY_DROPIN", tmp_path / "no-drop-in" / "10-ipv4-only.conf")
    # The test's files are not root's, and there is no caddy user to switch to: run as a user
    # would, even when the suite runs as root. Tests of root's rules patch this back.
    monkeypatch.setattr(caddy, "_is_root", lambda: False)
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", tmp_path / "no-etc-webspec" / "config.json")
    monkeypatch.setattr(caddy, "GATEWAY_ENV", tmp_path / "no-etc-webspec" / "gateway.env")
    monkeypatch.delenv("WEBSPEC_DOMAIN", raising=False)
    fake = FakeCaddy(tmp_path)
    monkeypatch.setenv("WEBSPEC_CADDY_BIN", str(fake.path))
    return CaddyHost(tmp_path, caddyfile, conf, fake)


@pytest.fixture(autouse=True)
def _linux(monkeypatch):
    """The Linux deployment, whatever this machine is; the macOS tests say so."""
    monkeypatch.setattr(caddy, "_on_macos", lambda: False)


@pytest.fixture
def host(tmp_path, monkeypatch) -> CaddyHost:
    return make_caddy_host(tmp_path, monkeypatch)


def _guarded(name: str = "svc", domain: str = "example.com") -> str:
    return generate_site_block(name, domain, guard=True)


# ── the caddy that runs: never from PATH, never replaceable by another user as root ──


def _planted_caddy(tmp_path: Path) -> tuple[Path, Path]:
    """A `caddy` in a directory of its own, which leaves a mark if it ever runs."""
    bindir = tmp_path / "agent-bin"
    bindir.mkdir()
    marker = tmp_path / "planted-caddy-ran"
    planted = bindir / "caddy"
    planted.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    planted.chmod(0o755)
    return bindir, marker


@pytest.mark.parametrize("root", [False, True])
def test_reload_never_looks_caddy_up_on_path(tmp_path, monkeypatch, root, caplog):
    # macOS sudo keeps the caller's PATH; a `caddy` the agent planted there must never run.
    bindir, marker = _planted_caddy(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("WEBSPEC_CADDY_BIN", raising=False)
    monkeypatch.setattr(caddy, "CADDY_BIN", tmp_path / "no-usr-local-bin" / "caddy")
    monkeypatch.setattr(caddy, "_is_root", lambda: root)
    real_run = subprocess.run
    argv0 = []

    def run(argv, *args, **kwargs):
        argv0.append(argv[0])
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(caddy.subprocess, "run", run)
    assert reload_caddy() is False
    assert argv0 == [] and not marker.exists()
    assert "does not exist" in caplog.text and "systemctl reload caddy-webspec" in caplog.text


def test_a_relative_caddy_is_refused(tmp_path, monkeypatch):
    bindir, marker = _planted_caddy(tmp_path)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("WEBSPEC_CADDY_BIN", "caddy")
    path, why = trusted_caddy()
    assert path is None and "not an absolute path" in why
    assert reload_caddy() is False and not marker.exists()


def test_as_root_a_caddy_another_user_can_replace_is_not_run(tmp_path, monkeypatch, caplog):
    # The test's own files are not root's: as far as root is concerned, the agent could swap them.
    bindir, marker = _planted_caddy(tmp_path)
    monkeypatch.setenv("WEBSPEC_CADDY_BIN", str(bindir / "caddy"))
    monkeypatch.setattr(caddy, "_is_root", lambda: True)
    path, why = trusted_caddy()
    assert path is None
    assert "can be modified by a user other than root" in why and str(bindir / "caddy") in why
    assert reload_caddy() is False and not marker.exists()


def test_as_root_status_refuses_an_untrusted_caddy(host, monkeypatch):
    monkeypatch.setattr(caddy, "_is_root", lambda: True)
    status, why = caddy_status()
    assert status == REFUSED and "can be modified by a user other than root" in why
    with pytest.raises(CaddyError):
        apply_site_changes({"svc": _guarded()})
    assert host.caddy.calls() == [] and not host.block("svc").exists()


def test_unsafe_component_accepts_root_owned_system_paths():
    for path in ("/usr/bin/env", "/bin/sh"):
        if os.path.exists(path):
            assert unsafe_component(Path(path)) is None, path


def test_unsafe_component_finds_what_others_can_modify(tmp_path):
    exe = tmp_path / "caddy"
    exe.write_text("")
    if os.geteuid() != 0:
        assert unsafe_component(exe) == exe  # the test user's file
    # /tmp is world-writable (sticky or not), behind a root-owned link on macOS: never trusted.
    assert unsafe_component(Path("/tmp/caddy")) is not None
    assert unsafe_component(Path("relative/caddy")) == Path("relative/caddy")
    assert unsafe_component(Path("/usr/bin/../bin/env")) is not None  # not normalized


def _inode(path: Path, *, link: bool = False) -> tuple[int, int]:
    st = os.lstat(path) if link else os.stat(path)
    return st.st_dev, st.st_ino


def test_unsafe_component_judges_the_physical_path_too(tmp_path, monkeypatch):
    # A root-owned /usr/local/bin/caddy that links into a directory another user can write is that
    # user's to replace: the components the link leads to count as much as the ones written. Who
    # may modify what is decided here by inode, so the test's own files can play both parts.
    written, elsewhere = tmp_path / "written", tmp_path / "elsewhere"
    written.mkdir()
    elsewhere.mkdir()
    target = elsewhere / "caddy"
    target.write_text("")
    link = written / "caddy"
    link.symlink_to(target)
    unsafe: set[tuple[int, int]] = set()
    monkeypatch.setattr(caddy, "_others_can_modify",
                        lambda st, *, link=False: (st.st_dev, st.st_ino) in unsafe)
    assert unsafe_component(link) is None
    unsafe.add(_inode(elsewhere))  # the directory of the link's target
    assert unsafe_component(link) == elsewhere
    unsafe.clear()
    unsafe.add(_inode(target))  # the target itself
    assert unsafe_component(link) == target
    unsafe.clear()
    unsafe.add(_inode(link, link=True))  # the link, as written
    assert unsafe_component(link) == link


def test_as_root_caddy_runs_as_the_caddy_user_without_roots_groups(tmp_path, monkeypatch):
    # Root never executes caddy, and the caddy it runs keeps none of root's supplementary groups.
    monkeypatch.setattr(caddy, "_is_root", lambda: True)

    def getpwnam(name):
        if name != "caddy":
            raise KeyError(name)
        return SimpleNamespace(pw_uid=996, pw_gid=995, pw_dir="/var/lib/caddy")

    monkeypatch.setattr(caddy.pwd, "getpwnam", getpwnam)
    seen: dict = {}

    def run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(caddy.subprocess, "run", run)
    assert caddy._run_caddy(Path("/usr/local/bin/caddy"), ["validate", "--config", "/x"], tmp_path) == (True, "")
    assert (seen["user"], seen["group"], seen["extra_groups"]) == (996, 995, [])
    assert seen["env"] == {"PATH": caddy.SAFE_PATH, "HOME": "/var/lib/caddy",
                           "XDG_DATA_HOME": "/var/lib/caddy-webspec/data",
                           "XDG_CONFIG_HOME": "/var/lib/caddy-webspec/config"}
    assert seen["cwd"] == "/" and seen["argv"] == ["/usr/local/bin/caddy", "validate", "--config", "/x"]


@pytest.mark.parametrize("mode,link,expected", [
    (0o755, False, False), (0o775, False, True), (0o757, False, True), (0o777, True, False),
])
def test_others_can_modify(mode, link, expected):
    st = os.stat_result((stat.S_IFREG | mode, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    assert caddy._others_can_modify(st, link=link) is expected
    st = os.stat_result((stat.S_IFREG | 0o755, 0, 0, 1, 1000, 0, 0, 0, 0, 0))  # owned by a user
    assert caddy._others_can_modify(st, link=link) is True


def test_caddy_gets_no_part_of_the_callers_environment(host, monkeypatch):
    monkeypatch.setenv("CADDY_ADMIN", "tcp/0.0.0.0:2019")
    monkeypatch.setenv("SOME_SECRET", "hunter2")
    apply_site_changes({"svc": _guarded()})
    for command in ("validate", "reload"):
        env = host.caddy.env(command)
        assert "CADDY_ADMIN" not in env and "SOME_SECRET" not in env
        assert env["PATH"] == caddy.SAFE_PATH
        assert {"HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"} <= set(env)


# ── caddy_status: which hosts webspec-ctl changes ──


def test_a_migrated_host_is_managed(host):
    assert caddy_status() == (MANAGED, "")


def test_macos_ships_no_proxy(host, monkeypatch):
    monkeypatch.setattr(caddy, "_on_macos", lambda: True)
    status, why = caddy_status()
    assert status == ABSENT and "macOS" in why and "no proxy" in why
    with pytest.raises(CaddyError):
        apply_site_changes({"svc": _guarded()})
    assert host.caddy.calls() == [] and not host.block("svc").exists()


def test_macos_never_runs_a_caddy(tmp_path, monkeypatch):
    # Homebrew's /usr/local/bin/caddy on an Intel Mac belongs to the login user.
    bindir, marker = _planted_caddy(tmp_path)
    monkeypatch.setattr(caddy, "_on_macos", lambda: True)
    monkeypatch.setattr(caddy, "CADDY_BIN", bindir / "caddy")
    monkeypatch.delenv("WEBSPEC_CADDY_BIN", raising=False)
    assert reload_caddy() is False and not marker.exists()


def test_no_caddyfile_means_no_caddy(tmp_path, monkeypatch):
    make_caddy_host(tmp_path, monkeypatch, domain=None)
    status, why = caddy_status()
    assert status == ABSENT and "setup-caddy.sh" in why


_OLD_LAYOUT = "{\n\tadmin localhost:2019\n\tauto_https off\n}\n\nimport /etc/caddy/conf.d/*.caddy\n\n:7001 {\n\trespond \"Unknown service\" 421\n}\n"


def test_the_pre_hardening_layout_is_refused_and_left_alone(host):
    # Its Caddy binds :7001 itself: a block that binds systemd's sockets would load nowhere and,
    # left on disk, keep Caddy from starting at its next restart.
    host.caddyfile.write_text(_OLD_LAYOUT)
    (host.block("old")).write_text("http://old.localhost:7001 {\n\treverse_proxy localhost:7002\n}\n")
    before = host.snapshot()
    status, why = caddy_status()
    assert status == REFUSED
    assert "pre-hardening Caddy layout" in why and "setup-caddy.sh" in why
    for changes in ({"svc": _guarded()}, {"old": None}):
        with pytest.raises(CaddyError, match="pre-hardening"):
            apply_site_changes(changes)
    assert host.snapshot() == before and host.caddy.calls() == []


def test_a_caddyfile_others_can_write_is_refused(host):
    host.caddyfile.chmod(0o666)
    assert caddy_status()[0] == REFUSED


def test_a_caddyfile_that_does_not_import_conf_d_is_refused(host):
    host.caddyfile.write_text(host.caddyfile.read_text().replace(f"import {host.conf}/*.caddy", ""))
    status, why = caddy_status()
    assert status == REFUSED and "does not import" in why


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_user_who_cannot_write_conf_d_is_told_to_use_sudo(host):
    host.conf.chmod(0o555)
    try:
        status, why = caddy_status()
    finally:
        host.conf.chmod(0o755)
    assert status == REFUSED and "as root" in why


# ── apply_site_changes: staged, validated, written, reloaded ──


def test_a_change_is_validated_on_a_staged_copy_then_written_and_reloaded(host):
    write_site_block("other", _guarded("other"), conf_dir=host.conf)
    assert apply_site_changes({"svc": _guarded()}) is True
    validate, reload = host.caddy.calls()
    assert validate.startswith("validate --config ") and not validate.endswith(str(host.caddyfile))
    assert reload == f"reload --config {host.caddyfile}"
    stage = host.caddy.stage
    assert sorted(p.name for p in (stage / "conf.d").iterdir()) == ["other.caddy", "svc.caddy"]
    staged_caddyfile = (stage / "Caddyfile").read_text()
    assert f"import {host.conf}/*.caddy" not in staged_caddyfile  # the copy imports its own blocks
    assert staged_caddyfile.count("/conf.d/*.caddy") == 1
    assert read_site(host.block("svc")) == Site("svc", "service", guard=True)


def test_validation_failure_changes_nothing(host):
    # caddy validate runs on the staged copy before any live file is touched.
    write_site_block("svc", _guarded(), conf_dir=host.conf)
    before = host.snapshot()
    host.caddy.fail("validate", '{"level":"error","msg":"adapting config using caddyfile","error":"bad zone"}\n'
                                "Error: adapting config using caddyfile: bad zone\n")
    with pytest.raises(CaddyError) as excinfo:
        apply_site_changes({"svc": _guarded(domain="other.dev"), "web": None, "new": _guarded("new")},
                           domain="other.dev")
    assert "rejects the new configuration" in str(excinfo.value) and "bad zone" in str(excinfo.value)
    assert host.snapshot() == before
    assert [c.split()[0] for c in host.caddy.calls()] == ["validate"]


def test_reload_failure_puts_every_file_back(host):
    # A rejected reload must never leave on disk a configuration that cannot start.
    old = _guarded()
    write_site_block("svc", old, conf_dir=host.conf)
    write_site_block("gone", _guarded("gone"), conf_dir=host.conf)
    os.chmod(host.block("gone"), 0o640)
    before = host.snapshot()
    modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in host.conf.iterdir()}
    host.caddy.fail("reload", '{"level":"error","msg":"sending configuration to instance: caddy responded with '
                              'error: HTTP 400: listening on fd/4: socket operation on non-socket"}\n')
    with pytest.raises(CaddyError) as excinfo:
        apply_site_changes({"svc": _guarded(domain="new.dev"), "gone": None, "added": _guarded("added")},
                           domain="new.dev")
    message = str(excinfo.value)
    assert "did not load the change" in message and "put back" in message and "fd/4" in message
    assert host.snapshot() == before
    assert {p.name: stat.S_IMODE(p.stat().st_mode) for p in host.conf.iterdir()} == modes
    assert not list(host.conf.glob(".*.tmp"))


def test_reload_failure_restores_a_symlinked_block(host, tmp_path):
    target = tmp_path / "hand-made.txt"
    target.write_text("hand made")
    host.block("svc").symlink_to(target)
    host.caddy.fail("reload", "boom")
    with pytest.raises(CaddyError):
        apply_site_changes({"svc": _guarded()})
    assert host.block("svc").is_symlink() and os.readlink(host.block("svc")) == str(target)
    assert target.read_text() == "hand made"


def test_a_stopped_caddy_is_named(host):
    host.caddy.fail("reload", '{"level":"error","msg":"sending configuration to instance: performing request: '
                              'Post \\"http://127.0.0.1/load\\": dial unix /run/caddy-webspec/admin.sock: '
                              'connect: no such file or directory"}\n')
    with pytest.raises(CaddyError, match="Caddy is not running"):
        apply_site_changes({"svc": _guarded()})
    assert not host.block("svc").exists()


def _reload_raises(monkeypatch, outcome):
    """Let `caddy validate` run; make `caddy reload` raise ``outcome`` or return it."""
    real = caddy._run_caddy

    def run(binary, args, scratch):
        if args[0] != "reload":
            return real(binary, args, scratch)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(caddy, "_run_caddy", run)


def test_an_interrupted_reload_puts_every_file_back(host, monkeypatch):
    write_site_block("svc", _guarded(), conf_dir=host.conf)
    before = host.snapshot()
    _reload_raises(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        apply_site_changes({"svc": None, "new": _guarded("new")}, domain="example.com")
    assert host.snapshot() == before


def test_a_reload_that_times_out_may_have_loaded_the_change(host, monkeypatch):
    _reload_raises(monkeypatch, (False, f"`caddy reload` {caddy._TIMED_OUT} 30 s"))
    with pytest.raises(CaddyError, match="may still have loaded the change") as excinfo:
        apply_site_changes({"svc": _guarded()})
    assert "systemctl reload caddy-webspec" in str(excinfo.value)
    assert not host.block("svc").exists()


def test_an_entry_caddy_would_import_that_is_not_a_regular_file_stops_the_change(host, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("not for the caddy user")
    host.block("linked").symlink_to(secret)
    before = host.snapshot()
    with pytest.raises(CaddyError, match="not a regular file"):
        apply_site_changes({"svc": _guarded()})
    assert host.snapshot() == before and host.caddy.calls() == []


def test_a_fifo_at_a_changed_path_stops_the_change(host):
    os.mkfifo(host.block("svc"))
    with pytest.raises(CaddyError, match="not a regular file"):
        apply_site_changes({"svc": _guarded()})
    assert stat.S_ISFIFO(os.lstat(host.block("svc")).st_mode) and host.caddy.calls() == []


def test_nothing_to_change_runs_nothing(host):
    write_site_block("svc", _guarded(), conf_dir=host.conf)
    assert apply_site_changes({"svc": _guarded(), "missing": None}) is False
    assert host.caddy.calls() == []


def test_a_removal_is_a_change_too(host):
    write_site_block("svc", _guarded(), conf_dir=host.conf)
    assert apply_site_changes({"svc": None}) is True
    assert not host.block("svc").exists()
    assert "svc.caddy" not in [p.name for p in (host.caddy.stage / "conf.d").iterdir()]


# ── the public domain the configuration records ──


def test_the_generated_caddyfile_records_its_domain_and_listeners():
    for domain in ("example.com", ""):
        line = generate_global_caddyfile(domain=domain, listen=DUAL).splitlines()[1]
        assert line == caddy.CADDYFILE_META_PREFIX + json.dumps({"domain": domain, "listeners": "dual"},
                                                                separators=(",", ":"))
    with pytest.raises(ValueError):
        generate_global_caddyfile(domain="Bad Domain", listen=DUAL)


def test_a_recorded_domain_is_reused_when_none_is_given(tmp_path, monkeypatch):
    # sudo drops WEBSPEC_DOMAIN: the routine command keeps serving what setup-caddy.sh was given.
    make_caddy_host(tmp_path, monkeypatch, domain="example.com")
    domain = resolve_domain()
    assert (domain.name, domain.explicit) == ("example.com", False)
    assert "recorded in" in domain.source


def test_the_environment_overrides_the_record_even_when_empty(tmp_path, monkeypatch):
    make_caddy_host(tmp_path, monkeypatch, domain="example.com")
    assert resolve_domain({"WEBSPEC_DOMAIN": "new.dev"}).name == "new.dev"
    domain = resolve_domain({"WEBSPEC_DOMAIN": ""})
    assert (domain.name, domain.explicit) == ("", True)


def test_a_caddyfile_from_before_the_record_has_its_domain_in_the_blocks(host):
    # setup-caddy.sh at fc0c558 wrote no record; the trusted blocks' public addresses tell.
    lines = host.caddyfile.read_text().split("\n")
    del lines[1]
    host.caddyfile.write_text("\n".join(lines))
    write_site_block("open", generate_site_block("open", "example.com", guard=False), conf_dir=host.conf)
    assert recorded_domain() == (None, "")
    write_site_block("svc", _guarded(), conf_dir=host.conf)
    write_site_block("web", generate_direct_site_block("web", "example.com", target_port=3000), conf_dir=host.conf)
    assert recorded_domain()[0] == "example.com"
    write_site_block("odd", _guarded("odd", "other.dev"), conf_dir=host.conf)
    with pytest.raises(DomainError, match="more than one public domain"):
        recorded_domain()


def test_apply_records_the_domain_in_the_caddyfile(host):
    apply_site_changes({}, domain="example.com")
    assert recorded_domain()[0] == "example.com"
    assert host.caddyfile.read_text().startswith(caddy.GENERATED_MARKER)
    # An older Caddyfile gets the line; other recorded fields are kept.
    lines = host.caddyfile.read_text().split("\n")
    lines[1] = caddy.CADDYFILE_META_PREFIX + '{"domain":"example.com","extra":[3,4],"listeners":"dual"}'
    host.caddyfile.write_text("\n".join(lines))
    apply_site_changes({}, domain="")
    assert json.loads(host.caddyfile.read_text().split("\n")[1][len(caddy.CADDYFILE_META_PREFIX):]) == \
        {"domain": "", "extra": [3, 4], "listeners": "dual"}


def test_a_malformed_record_needs_an_explicit_domain(host):
    lines = host.caddyfile.read_text().split("\n")
    lines[1] = caddy.CADDYFILE_META_PREFIX + '{"domain": 7, "listeners": "dual"}'
    host.caddyfile.write_text("\n".join(lines))
    with pytest.raises(DomainError):
        resolve_domain()
    assert resolve_domain({"WEBSPEC_DOMAIN": "example.com"}).name == "example.com"
    apply_site_changes({}, domain="example.com")  # the explicit domain replaces the record
    assert recorded_domain()[0] == "example.com"
    assert host.caddyfile.read_text().count(caddy.CADDYFILE_META_PREFIX) == 1


def _production(tmp_path, monkeypatch, env_text: str) -> Path:
    production = tmp_path / "etc-webspec"
    production.mkdir()
    (production / "config.json").write_text("{}")
    (production / "gateway.env").write_text(env_text)
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", production / "config.json")
    monkeypatch.setattr(config_writer, "PRODUCTION_ENV", production / "gateway.env")
    monkeypatch.setattr(caddy, "GATEWAY_ENV", production / "gateway.env")
    return production / "gateway.env"


@pytest.mark.parametrize("line", ["WEBSPEC_DOMAIN=example.com", "export WEBSPEC_DOMAIN='example.com'"])
def test_production_gateway_env_is_the_gateways_own_setting(tmp_path, monkeypatch, line):
    # Before setup-caddy.sh has recorded anything: its first run serves the gateway's domain.
    # The macOS gateway.env, which sh sources: `export NAME=value` sets NAME too.
    monkeypatch.setattr(config_writer, "env_file_sourced", lambda path=None: True)
    make_caddy_host(tmp_path, monkeypatch, domain=None)
    env = _production(tmp_path, monkeypatch, line + "\n")
    assert (resolve_domain().name, resolve_domain().source) == ("example.com", str(env))


def test_on_linux_an_export_line_is_not_the_gateways_setting(tmp_path, monkeypatch, caplog):
    # P11: systemd's EnvironmentFile= ignores `export WEBSPEC_DOMAIN=…`, so the gateway never gets
    # the domain: Caddy must not serve it either (the gateway would answer 404), and the operator
    # hears why, without the line's value.
    monkeypatch.setattr(config_writer, "env_file_sourced", lambda path=None: False)
    make_caddy_host(tmp_path, monkeypatch, domain=None)
    env = _production(tmp_path, monkeypatch, "WEBSPEC_GUARD_KEY=k\nexport WEBSPEC_DOMAIN=example.com\n")
    assert (resolve_domain().name, resolve_domain().source) == ("", "not set")
    warnings = [r.getMessage() for r in caplog.records if "systemd ignores" in r.getMessage()]
    assert warnings == [f"{env} sets WEBSPEC_DOMAIN only with `export WEBSPEC_DOMAIN=…` (line 2), which systemd "
                        "ignores: it reads KEY=value lines only, and logs that line, value included, to the journal "
                        "at every start. The gateway does not get WEBSPEC_DOMAIN from this file. Change line 2 to "
                        "WEBSPEC_DOMAIN=value."]  # once, though read twice
    assert "example.com" not in warnings[0]
    env.write_text("export WEBSPEC_DOMAIN=example.com\nWEBSPEC_DOMAIN=example.org\n")
    assert resolve_domain().name == "example.org"  # the line systemd reads


def test_on_linux_the_restart_hint_needs_the_line_systemd_reads(tmp_path, monkeypatch):
    # P11: "already sets it, restart" was false for an export line: the restart changed nothing.
    monkeypatch.setattr(config_writer, "env_file_sourced", lambda path=None: False)
    make_caddy_host(tmp_path, monkeypatch, domain=None)
    _production(tmp_path, monkeypatch, "export WEBSPEC_DOMAIN=example.com\n")
    hint = caddy.gateway_domain_hint("example.com")
    assert "Set WEBSPEC_DOMAIN=example.com in" in hint and "already sets" not in hint


def test_a_recorded_opt_out_outlasts_the_gateways_own_domain(tmp_path, monkeypatch):
    # `caddy-sync --no-public` (or a setup without a domain) records none. A production gateway
    # keeps WEBSPEC_DOMAIN in gateway.env, and must not put the public addresses back by itself.
    make_caddy_host(tmp_path, monkeypatch, domain="")
    env = _production(tmp_path, monkeypatch, "WEBSPEC_DOMAIN=example.com\n")
    domain = resolve_domain()
    assert (domain.name, domain.explicit) == ("", False) and "recorded in" in domain.source
    assert f"{env} sets WEBSPEC_DOMAIN=example.com" in domain.note
    assert "caddy-sync with WEBSPEC_DOMAIN=example.com" in domain.note
    assert resolve_domain({"WEBSPEC_DOMAIN": "example.com"}).name == "example.com"  # only when asked


def test_a_gateway_without_the_recorded_domain_is_pointed_out(tmp_path, monkeypatch):
    # Caddy forwards the recorded domain's hosts to a gateway that does not route them: 404.
    make_caddy_host(tmp_path, monkeypatch, domain="example.com")
    _production(tmp_path, monkeypatch, "WEBSPEC_DOMAIN=\n")
    domain = resolve_domain()
    assert domain.name == "example.com" and "404" in domain.note and "WEBSPEC_DOMAIN=example.com" in domain.note
    make_caddy_host(tmp_path / "dev", monkeypatch, domain="example.com")  # no gateway.env to compare
    assert resolve_domain().note == ""


def test_an_empty_production_setting_keeps_the_recorded_domain(tmp_path, monkeypatch):
    # install.sh writes `WEBSPEC_DOMAIN=` as its default: that drops nothing by itself.
    make_caddy_host(tmp_path, monkeypatch, domain="example.com")
    _production(tmp_path, monkeypatch, "WEBSPEC_DOMAIN=\n")
    assert resolve_domain().name == "example.com"


def test_a_production_setting_that_contradicts_the_record_must_be_settled(tmp_path, monkeypatch):
    make_caddy_host(tmp_path, monkeypatch, domain="example.com")
    _production(tmp_path, monkeypatch, "WEBSPEC_DOMAIN=new.dev\n")
    with pytest.raises(DomainError, match="WEBSPEC_DOMAIN=new.dev"):
        resolve_domain()
    assert resolve_domain({"WEBSPEC_DOMAIN": "new.dev"}).name == "new.dev"


def test_gateway_env_is_not_read_on_a_development_host(tmp_path, monkeypatch):
    # /etc/webspec without config.json: not the env file of the gateway that runs here.
    make_caddy_host(tmp_path, monkeypatch, domain="")
    stray = tmp_path / "no-etc-webspec"
    stray.mkdir()
    (stray / "gateway.env").write_text("WEBSPEC_DOMAIN=stray.dev\n")
    assert resolve_domain().name == ""


# (line, as sh reads it (the macOS gateway.env), as systemd reads it (an EnvironmentFile=, Linux))
@pytest.mark.parametrize("line,sourced,systemd", [
    ("export WEBSPEC_DOMAIN=example.com", "example.com", None),
    ("export   WEBSPEC_DOMAIN='example.com'", "example.com", None),
    ("  export WEBSPEC_DOMAIN=\"example.com\"", "example.com", None),
    ("exportWEBSPEC_DOMAIN=example.com", None, None),
    ("# export WEBSPEC_DOMAIN=example.com", None, None),
    ("WEBSPEC_DOMAIN=example.com", "example.com", "example.com"),
    ("  WEBSPEC_DOMAIN = 'example.com'", "example.com", "example.com"),
    ("WEBSPEC_DOMAIN=", "", ""),
])
def test_env_file_values_as_their_consumer_reads_them(tmp_path, monkeypatch, line, sourced, systemd):
    path = tmp_path / "gateway.env"
    path.write_text(line + "\n")
    assert read_env_file_value(path, "WEBSPEC_DOMAIN", sourced=True) == sourced
    assert read_env_file_value(path, "WEBSPEC_DOMAIN", sourced=False) == systemd
    # By default, as the production gateway.env is read on this platform: sh on macOS, systemd on
    # Linux. Any other file, which no unit reads, the way a shell does (config_writer.env_file_sourced).
    monkeypatch.setattr(config_writer, "PRODUCTION_ENV", path)
    for platform, expected in (("darwin", sourced), ("linux", systemd)):
        monkeypatch.setattr(config_writer.sys, "platform", platform)
        assert read_env_file_value(path, "WEBSPEC_DOMAIN") == expected
    other = tmp_path / "other.env"
    other.write_text(line + "\n")
    assert read_env_file_value(other, "WEBSPEC_DOMAIN") == sourced


# ── optional: the real Caddy rejects what it cannot load ──


@pytest.mark.skipif(not os.environ.get("WEBSPEC_TEST_CADDY"), reason="set WEBSPEC_TEST_CADDY to a caddy binary")
def test_real_caddy_validates_the_staged_copy(tmp_path, monkeypatch):
    host = make_caddy_host(tmp_path, monkeypatch, domain="example.com")
    monkeypatch.setenv("WEBSPEC_CADDY_BIN", os.environ["WEBSPEC_TEST_CADDY"])
    log_dir = tmp_path / "log"
    good = generate_site_block("svc", "example.com", guard=True, log_dir=log_dir)
    broken = good.replace("\treverse_proxy 127.0.0.1:7002", "\treverse_proxy 127.0.0.1:7002 {\n\t\tbogus\n\t}")
    with pytest.raises(CaddyError, match="rejects the new configuration"):
        apply_site_changes({"svc": broken})
    assert not host.block("svc").exists()
    # A valid change passes validation; with no Caddy running, the reload fails and is undone.
    with pytest.raises(CaddyError, match="did not load the change"):
        apply_site_changes({"svc": good})
    assert not host.block("svc").exists()
