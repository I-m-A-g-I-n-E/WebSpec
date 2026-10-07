"""build-caddy.sh builds into, and runs, only a binary that no user but root and the caller can change.

DP-1 puts the agent on the same host as another user, and `check` runs the binary it is given:
one the agent could swap would run the agent's program as the operator (or as root, if it went
on to setup-caddy.sh). Owners need a second user, so these tests vary the mode bits, which the
same checks read; run as root, they also give a directory to another owner. A fake caddy logs
every run, so a refusal is seen to come before the binary runs, and `build` meets a fake curl
before it could download anything.

What CI builds and requires (the pins, the Go checksums, the modules) is read from setup-caddy.sh;
the last tests run a copy of the script beside an edited copy of setup-caddy.sh.

Run from the repository root: python -m pytest -q -p no:cacheprovider .github/scripts
"""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

SCRIPT = Path(__file__).resolve().with_name("build-caddy.sh")
SETUP_SCRIPT = SCRIPT.parents[2] / "gateway" / "tools" / "setup-caddy.sh"
BASH = shutil.which("bash")
UNAME = shutil.which("uname")

pytestmark = pytest.mark.skipif(BASH is None or UNAME is None or shutil.which("find") is None,
                                reason="needs bash, uname and find")


def run(*args, cwd=None, path_prefix=None, script=SCRIPT):
    env = dict(os.environ)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    return subprocess.run([BASH, str(script), *args], cwd=cwd, env=env, capture_output=True,
                          encoding="utf-8", timeout=60)


class Pins(NamedTuple):
    caddy: str
    plugin: str
    plugin_version: str
    modules: tuple  # the modules setup-caddy.sh requires of a Caddy


def read_pins(script=SCRIPT) -> Pins:
    """What `key` reads from setup-caddy.sh."""
    r = run("key", script=script)
    if r.returncode != 0 and shutil.which("sha256sum") is None:
        pytest.skip("`key` needs sha256sum")
    assert r.returncode == 0, r.stderr
    m = re.search(r"Caddy (\S+) with (\S+)@(\S+),", r.stderr)
    modules = re.search(r"^Modules setup-caddy.sh requires: (.+)$", r.stderr, re.M)
    assert m and modules, r.stderr
    return Pins(*m.groups(), tuple(modules.group(1).split()))


@pytest.fixture(scope="module")
def pins():
    return read_pins()


def fake_caddy(path: Path, log: Path, pins=Pins("v0", "example.com/plugin", "v0", ("http.handlers.rate_limit",)),
               mode=0o755) -> Path:
    """A program that prints what `check` expects of a build with these pins, and logs every run."""
    path.write_text(f"""#!/bin/sh
echo "$*" >> '{log}'
case "$1" in
version) echo '{pins.caddy} h1:fake' ;;
list-modules) printf '%s\\n' {' '.join(pins.modules)} ;;
build-info) printf 'dep\\t%s\\t%s\\th1:fake\\n' '{pins.plugin}' '{pins.plugin_version}' ;;
esac
""")
    path.chmod(mode)
    return path


def directory(path: Path, mode: int) -> Path:
    path.mkdir()
    path.chmod(mode)  # mkdir's mode is filtered by the umask; chmod's is not
    return path


def test_check_runs_a_binary_that_only_the_caller_can_change(tmp_path, pins):
    log = tmp_path / "runs.log"
    binary = fake_caddy(directory(tmp_path / "private", 0o700) / "caddy", log, pins)
    r = run("check", str(binary))
    assert r.returncode == 0, r.stderr
    assert f"{binary}: Caddy {pins.caddy} h1:fake, with {pins.plugin}@{pins.plugin_version}" in r.stdout
    assert log.read_text().split() == ["version", "list-modules", "build-info"]


def test_check_is_a_check_against_the_pins_only(tmp_path, pins):
    """Any program that prints the pinned strings passes, so it is no integrity check; that is
    why where the binary lives is checked before it runs."""
    log = tmp_path / "runs.log"
    binary = fake_caddy(directory(tmp_path / "private", 0o700) / "caddy", log, pins._replace(caddy="v0.0.1"))
    r = run("check", str(binary))
    assert r.returncode == 1
    assert f"is Caddy v0.0.1 h1:fake, not {pins.caddy}" in r.stderr


def test_a_sticky_shared_directory_may_be_above_the_binary(tmp_path, pins):
    """Like /tmp above mktemp -d: others can add entries there, but not rename or remove ours."""
    log = tmp_path / "runs.log"
    shared = directory(tmp_path / "shared", 0o1777)
    binary = fake_caddy(directory(shared / "mine", 0o700) / "caddy", log, pins)
    r = run("check", str(binary))
    assert r.returncode == 0, r.stderr
    assert log.exists()


def test_check_follows_links_that_only_root_or_the_caller_can_change(tmp_path, pins):
    """A link to a link in a sticky directory (which no one else can replace there), relative
    to it, that leads to a private directory."""
    log = tmp_path / "runs.log"
    private = directory(tmp_path / "private", 0o700)
    fake_caddy(private / "caddy", log, pins)
    hop = directory(tmp_path / "sticky", 0o1777) / "hop"
    hop.symlink_to(Path("..") / "private")
    (tmp_path / "link").symlink_to(hop)
    r = run("check", str(tmp_path / "link" / "caddy"))
    assert r.returncode == 0, r.stderr
    assert log.exists()


def _shared_directory(mode):
    def layout(tmp):
        d = directory(tmp / "shared", mode)
        return d / "caddy", d, 0o755
    return layout


def _shared_above(tmp):
    above = directory(tmp / "above", 0o777)  # not sticky: others can rename what it holds
    return directory(above / "mine", 0o700) / "caddy", above, 0o755


def _writable_binary(mode):
    def layout(tmp):
        binary = directory(tmp / "private", 0o700) / "caddy"
        return binary, binary, mode
    return layout


def _chain_of_links(tmp):
    """A link to a link that sits in a world-writable directory, though both lead to a private
    one: whoever replaces the second link chooses the binary. Every directory the kernel passes
    through counts, not only those of the path as written and of the physical path it ends at."""
    real = directory(tmp / "real", 0o700)
    hop = directory(tmp / "open", 0o777) / "hop"
    hop.symlink_to(real)
    start = directory(tmp / "start", 0o700) / "link"
    start.symlink_to(hop)
    return start / "caddy", tmp / "open", 0o755


UNSAFE = {
    "its directory is world-writable": _shared_directory(0o777),
    "its directory is group-writable": _shared_directory(0o770),
    # The binary's own directory may not be shared, even when it is sticky like /tmp.
    "its directory is sticky and world-writable": _shared_directory(0o1777),
    "a directory above is world-writable": _shared_above,
    "the binary is group-writable": _writable_binary(0o775),
    "the binary is world-writable": _writable_binary(0o757),
    "a link on the way is in a world-writable directory": _chain_of_links,
}


@pytest.mark.parametrize("layout", UNSAFE)
def test_check_refuses_before_running_a_binary_others_could_swap(tmp_path, layout):
    binary, culprit, mode = UNSAFE[layout](tmp_path)
    log = tmp_path / "runs.log"
    fake_caddy(binary, log, mode=mode)
    r = run("check", str(binary))
    assert r.returncode == 1
    assert f"refusing to run {binary}" in r.stderr
    # Named as the kernel reaches it (on macOS, /var is a link to /private/var).
    assert f"can change {culprit} (" in r.stderr or f"can change {culprit.resolve()} (" in r.stderr, r.stderr
    assert not log.exists()  # it never ran, not even to print its version


def test_check_refuses_a_link_to_the_binary(tmp_path):
    log = tmp_path / "runs.log"
    binary = fake_caddy(directory(tmp_path / "private", 0o700) / "caddy", log)
    link = tmp_path / "private" / "caddy-link"
    link.symlink_to(binary)
    r = run("check", str(link))
    assert r.returncode == 1
    assert "is a symbolic link" in r.stderr
    assert not log.exists()


def test_check_never_looks_the_binary_up_on_path(tmp_path):
    """`check caddy` means ./caddy, the file it examined, not a caddy found on PATH."""
    here_log, path_log = tmp_path / "here.log", tmp_path / "path.log"
    cwd = directory(tmp_path / "cwd", 0o700)
    fake_caddy(cwd / "caddy", here_log)
    on_path = directory(tmp_path / "bin", 0o700)
    fake_caddy(on_path / "caddy", path_log)
    run("check", "caddy", cwd=cwd, path_prefix=on_path)
    assert here_log.exists()
    assert not path_log.exists()


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="giving a file away needs root")
@pytest.mark.parametrize("theirs", ["directory", "link in a sticky directory", "binary"])
def test_check_refuses_what_another_user_owns(tmp_path, theirs):
    """nobody stands in for the agent: it could swap what it owns (a link, by replacing it)."""
    log = tmp_path / "runs.log"
    private = directory(tmp_path / "private", 0o755)
    binary = fake_caddy(private / "caddy", log)
    culprit = {"directory": private, "binary": binary,
               "link in a sticky directory": directory(tmp_path / "sticky", 0o1777) / "link"}[theirs]
    if theirs.startswith("link"):
        culprit.symlink_to(private)
        binary = culprit / "caddy"
    os.lchown(culprit, 65534, 65534)
    r = run("check", str(binary))
    assert r.returncode == 1
    assert f"can change {culprit} (" in r.stderr
    assert not log.exists()


# build stops at a fake curl, so nothing is downloaded. A fake uname says Linux, so that the
# location checks, which come before anything else, also run on macOS.


@pytest.fixture
def fakes(tmp_path):
    bin_dir = directory(tmp_path / "fakes", 0o700)
    curl_log = tmp_path / "curl.log"
    (bin_dir / "curl").write_text(f"#!/bin/sh\necho \"$*\" >> '{curl_log}'\nexit 97\n")
    (bin_dir / "uname").write_text(f"#!/bin/sh\nif [ \"$1\" = -s ]; then echo Linux; else exec '{UNAME}' \"$@\"; fi\n")
    for fake in bin_dir.iterdir():
        fake.chmod(0o755)
    return bin_dir, curl_log


def build(output: Path, fakes):
    """(the run, whether it reached the download)"""
    bin_dir, curl_log = fakes
    r = run("build", str(output), path_prefix=bin_dir)
    return r, curl_log.exists()


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o1777], ids=["world-writable", "group-writable", "sticky"])
def test_build_refuses_a_directory_others_can_write(tmp_path, fakes, mode):
    shared = directory(tmp_path / "shared", mode)
    r, downloading = build(shared / "caddy", fakes)
    assert r.returncode == 1
    assert f"refusing to build into {shared}" in r.stderr
    assert 'out="$(mktemp -d)/caddy"' in r.stderr  # and says what to use instead
    assert not downloading


def test_build_creates_nothing_below_a_directory_others_can_change(tmp_path, fakes):
    """The trap of a fixed path such as /tmp/caddy: another user made the directory first."""
    squatted = directory(tmp_path / "squatted", 0o777)
    r, downloading = build(squatted / "sub" / "caddy", fakes)
    assert r.returncode == 1
    assert "refusing" in r.stderr
    assert not (squatted / "sub").exists()
    assert not downloading


def test_build_creates_missing_directories_private(tmp_path, fakes):
    out = tmp_path / "new" / "sub" / "caddy"
    r, downloading = build(out, fakes)
    assert r.returncode == 97, r.stderr  # the fake curl: the location was accepted
    assert downloading
    for d in (tmp_path / "new", tmp_path / "new" / "sub"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_build_accepts_mktemp_d_below_a_sticky_directory(tmp_path, fakes):
    """The documented form, out="$(mktemp -d)/caddy", in a shared, sticky directory like /tmp."""
    shared = directory(tmp_path / "tmp", 0o1777)
    made = subprocess.run(["mktemp", "-d", f"{shared}/build.XXXXXX"], capture_output=True, text=True, check=True)
    r, downloading = build(Path(made.stdout.strip()) / "caddy", fakes)
    assert r.returncode == 97, r.stderr
    assert downloading


# What CI builds and requires is read from setup-caddy.sh, never written in build-caddy.sh. These
# run a copy of build-caddy.sh beside an edited copy of setup-caddy.sh.

MODULE_LOOP = r"^[ \t]*for module in [^\n]*; do[^\n]*$"


def beside_setup_script(tmp_path, *edits) -> Path:
    """A copy of build-caddy.sh, in a tree whose setup-caddy.sh is the real one with edits applied."""
    scripts, tools = tmp_path / "repo" / ".github" / "scripts", tmp_path / "repo" / "gateway" / "tools"
    scripts.mkdir(parents=True)
    tools.mkdir(parents=True)
    shutil.copy(SCRIPT, scripts / SCRIPT.name)
    text = SETUP_SCRIPT.read_text()
    for edit in edits:
        text = edit(text)
    (tools / SETUP_SCRIPT.name).write_text(text)
    return scripts / SCRIPT.name


def replace_line(pattern, new):
    """An edit that replaces the one line that matches pattern (a regular expression) with new."""
    def edit(text):
        changed, n = re.subn(pattern, lambda _: new, text, flags=re.M)
        assert n == 1, f"{pattern!r} matches {n} lines of setup-caddy.sh"
        return changed
    return edit


def duplicate_line(pattern):
    def edit(text):
        changed, n = re.subn(pattern, lambda m: f"{m.group(0)}\n{m.group(0)}", text, count=1, flags=re.M)
        assert n == 1, f"{pattern!r} matches no line of setup-caddy.sh"
        return changed
    return edit


def test_check_requires_the_modules_that_setup_caddy_requires(tmp_path):
    """A module setup-caddy.sh starts to require (http.handlers.map, say), CI's check requires too."""
    script = beside_setup_script(
        tmp_path, replace_line(MODULE_LOOP, "for module in http.handlers.rate_limit dns.providers.example; do"))
    pins = read_pins(script)
    assert pins.modules == ("http.handlers.rate_limit", "dns.providers.example")
    log = tmp_path / "runs.log"
    binary = directory(tmp_path / "private", 0o700) / "caddy"
    fake_caddy(binary, log, pins._replace(modules=("http.handlers.rate_limit", "caddy.logging.encoders.filter")))
    r = run("check", str(binary), script=script)
    assert r.returncode == 1
    assert "lacks the module dns.providers.example" in r.stderr
    fake_caddy(binary, log, pins)
    r = run("check", str(binary), script=script)
    assert r.returncode == 0, r.stderr


def test_key_reads_respaced_lines_that_end_in_a_comment(tmp_path):
    script = beside_setup_script(
        tmp_path,
        replace_line(MODULE_LOOP, "    for module in  a.b   c_d.e-f ;  do  # what caddy must have"),
        replace_line(r"^CADDY_VERSION=.*$", 'CADDY_VERSION="${CADDY_VERSION:-v2.99.1}"   # the Caddy'))
    pins = read_pins(script)
    assert pins.caddy == "v2.99.1"
    assert pins.modules == ("a.b", "c_d.e-f")


REFUSED = {
    "no module loop": ([replace_line(MODULE_LOOP, "")], 'expected one loop "for module in'),
    "two module loops": ([duplicate_line(MODULE_LOOP)], "found 2"),
    "the modules in a variable": ([replace_line(MODULE_LOOP, "for module in $MODULES; do")],
                                  "unexpected module $MODULES"),
    "no modules": ([replace_line(MODULE_LOOP, "for module in ; do")], "expected the modules on one line"),
    "the modules over two lines": (
        [replace_line(MODULE_LOOP, "for module in http.handlers.rate_limit \\\n    caddy.logging.encoders.filter; do")],
        "expected the modules on one line"),
    "a pin twice": ([duplicate_line(r"^CADDY_VERSION=.*$")], "expected one line CADDY_VERSION="),
    "a pin that is no version": ([replace_line(r"^CADDY_VERSION=.*$", 'CADDY_VERSION="${CADDY_VERSION:-latest}"')],
                                 "unexpected CADDY_VERSION=latest"),
    "a Go bump without its checksums": ([replace_line(r"^GO_VERSION=.*$", 'GO_VERSION="${GO_VERSION:-1.99.0}"')],
                                        "expected one checksum for Go 1.99.0"),
}


@pytest.mark.parametrize("change", REFUSED)
def test_key_fails_on_a_setup_script_it_cannot_read_unambiguously(tmp_path, change):
    """CI's first step fails loudly; it never guesses, nor skips a check."""
    edits, message = REFUSED[change]
    r = run("key", script=beside_setup_script(tmp_path, *edits))
    assert r.returncode == 1
    assert message in r.stderr, r.stderr
    assert "key=" not in r.stdout
