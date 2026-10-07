"""The Tests workflow runs the tests it relies on, and its Caddy is the one it checked.

The pytest job keeps Caddy where build-caddy.sh accepts it on any runner: right below /tmp, root's
and sticky everywhere, as build-caddy.sh refuses a path that a user other than root and the
caller can change (DP-1), which RUNNER_TEMP is on some runners (larger runners run with umask 002;
the runner's container image makes /home/runner 0777). The cache, the build, the check and the
tests use that one path, so the tests run the binary that was checked; and require-tests.py fails
a job whose required tests skipped, which pytest alone would pass. Each check is one command whose
failure fails its job, and the job's failure the run: none of them, the gate least of all, may
fail open (`|| true`, continue-on-error, an if).

The pytest job's gate requires the gateway's tests that need the real Caddy, those that read
WEBSPEC_TEST_CADDY: each of them in the report must have passed, and each module of them must be
in it. So the job fails when one of them skips, and when a module of them skips whole or drops
out of the run (a rename, --ignore, --deselect, -k, collect_ignore); a test that drops out of a
module whose other tests still run goes unnoticed. The job runs the suite on every Python from
the oldest that the gateway admits to 3.13.

Run from the repository root: python -m pytest -q -p no:cacheprovider .github/scripts
"""

import ast
import os
import re
import shlex
import tomllib
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "tests.yml"
GATEWAY = Path(__file__).resolve().parents[2] / "gateway"


@pytest.fixture(scope="module")
def jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def step(steps, predicate):
    """The index of the one step that satisfies predicate."""
    found = [i for i, s in enumerate(steps) if predicate(s)]
    assert len(found) == 1, f"{len(found)} steps match"
    return found[0]


def runs(text):
    return lambda s: text in s.get("run", "")


def command(step):
    """The words of step's run, which is one command whose exit status is the step's: a shell
    operator (||, ;, &&, |, &) would join another command to it, continue-on-error would let the
    job go on when it fails, an if could skip it, and a shell of its own could ignore its status."""
    assert not {"continue-on-error", "if", "shell"} & set(step), step
    run = step["run"].replace("\\\n", " ").strip()
    lexer = shlex.shlex(run, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    words = list(lexer)
    assert "\n" not in run and all(word.strip("();<>|&") for word in words), f"not one command: {run!r}"
    return words


def gate(steps):
    """The job's require-tests.py step: (its index, the report it reads, the names it requires
    with --ran, whether it passes --no-skips). It runs require-tests.py alone, with no options but
    those two, so that nothing else decides whether it fails."""
    index = step(steps, runs("require-tests.py"))
    words = command(steps[index])
    assert words[:2] == ["python", ".github/scripts/require-tests.py"], words
    report, *options = words[2:]
    assert not report.startswith("-"), words
    names, no_skips = [], False
    while options:
        option = options.pop(0)
        if option == "--no-skips":
            no_skips = True
        else:
            assert option == "--ran" and options and not options[0].startswith("-"), \
                f"require-tests.py {option}: only --ran NAME and --no-skips"
            names.append(options.pop(0))
    return index, report, names, no_skips


def test_caddy_lives_right_below_tmp(jobs):
    where = PurePosixPath(jobs["pytest"]["env"]["CI_CADDY_DIR"])
    assert where.parent == PurePosixPath("/tmp")
    assert "$" not in str(where)  # a fixed path: the cache restores and saves it by name


def test_the_cache_the_build_the_check_and_the_tests_use_one_caddy(jobs):
    steps = jobs["pytest"]["steps"]
    restore = step(steps, lambda s: s.get("uses", "").startswith("actions/cache/restore@"))
    save = step(steps, lambda s: s.get("uses", "").startswith("actions/cache/save@"))
    build, check = step(steps, runs("build-caddy.sh build")), step(steps, runs("build-caddy.sh check"))
    tests = step(steps, lambda s: "WEBSPEC_TEST_CADDY" in s.get("env", {}))
    for i in (restore, save):
        assert steps[i]["with"]["path"] == "${{ env.CI_CADDY_DIR }}"
    for i in (build, check):
        assert steps[i]["run"].split()[-1] == '"$CI_CADDY_DIR/caddy"'
    assert steps[tests]["env"]["WEBSPEC_TEST_CADDY"] == "${{ env.CI_CADDY_DIR }}/caddy"
    # Only a checked binary is saved or run, and the check runs on a restored one too.
    assert restore < build < check < save < tests
    assert "if" not in steps[check]


@pytest.mark.parametrize("job, tests, required, no_skips", [
    ("pytest", "python -m pytest", {"test_caddy_limits", "test_real_caddy"}, False),
    ("docs", "docs/tests", {"test_rendering"}, True)])
def test_a_job_fails_unless_its_required_tests_ran(jobs, job, tests, required, no_skips):
    steps = jobs[job]["steps"]
    run_tests = step(steps, lambda s: tests in s.get("run", "") and "--junitxml=" in s["run"])
    index, report, names, skips_fail = gate(steps)
    assert index > run_tests
    assert f"--junitxml={report}" in command(steps[run_tests])  # the report the tests wrote
    assert required <= set(names)
    assert skips_fail or not no_skips


@pytest.mark.parametrize("job, check", [
    ("pytest", "-p no:cacheprovider .github/scripts"),  # the tests of the CI scripts and of this file
    ("pytest", "build-caddy.sh check"),
    ("pytest", "--junitxml="),
    ("pytest", "require-tests.py"),
    ("docs", "mkdocs build --strict"),
    ("docs", "--junitxml="),
    ("docs", "require-tests.py")])
def test_a_check_that_fails_fails_the_run(jobs, job, check):
    """A check guards only what its failure stops: the job runs it as one command whose exit
    status is the step's, and the job's failure is the run's. `|| true` on its line, or
    continue-on-error or an if on its step or its job, would let the run pass without it."""
    steps = jobs[job]["steps"]
    command(steps[step(steps, runs(check))])
    assert not {"continue-on-error", "if"} & set(jobs[job])


# The gateway's tests, by the ids that the gate reads in the JUnit report: pytest run from
# gateway/, as the pytest job runs it, names a test "tests.MODULE.TEST" (with its parameters in
# brackets) or "tests.MODULE.CLASS.TEST", and a module that skips whole "tests.MODULE".

# The directories that pytest does not collect from: its default norecursedirs, and __pycache__.
NORECURSE = ("*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}", "__pycache__")


@pytest.fixture(scope="module")
def files():
    """(id, syntax tree, whether pytest collects it as a test module) of each Python file below
    gateway/ that pytest reaches, run there as the job runs it: all but those in a directory that
    it does not collect from or in a virtual environment. A test module is test_*.py or *_test.py,
    pytest's default python_files. A file with a null byte is no Python, as CPython runs no such
    source (the ._ files that macOS tar adds for extended attributes, say), and reads nothing."""
    found = []
    for top, dirs, names in os.walk(GATEWAY):
        dirs[:] = sorted(d for d in dirs if not any(fnmatchcase(d, pattern) for pattern in NORECURSE)
                         and not os.path.exists(os.path.join(top, d, "pyvenv.cfg")))
        for name in sorted(name for name in names if name.endswith(".py")):
            path = Path(top, name)
            source = path.read_bytes()
            if b"\0" not in source:
                found.append((".".join(path.relative_to(GATEWAY).with_suffix("").parts),
                              ast.parse(source, str(path)),
                              fnmatchcase(name, "test_*.py") or fnmatchcase(name, "*_test.py")))
    return found


def _reads_caddy(node):
    """Whether node reads WEBSPEC_TEST_CADDY: has that name as a string of its own, as the
    environment is read, and not inside another string, as a docstring or a skip reason has it."""
    return any(isinstance(n, ast.Constant) and n.value == "WEBSPEC_TEST_CADDY" for n in ast.walk(node))


def _is_test(statement):
    return isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name.startswith("test")


def _is_test_class(statement):
    return isinstance(statement, ast.ClassDef) and statement.name.startswith("Test")


def _tests(scope, statements):
    """{id: whether it needs WEBSPEC_TEST_CADDY} for scope, a module or a test class, and for each
    test and test class among its statements. A test needs it when it reads it (its decorators
    too), or when its scope does outside its tests, in a skip mark, a fixture or a helper, as then
    any test there may depend on it; and such a scope needs it."""
    found, reads = {}, False
    for statement in statements:
        if _is_test(statement):
            found[f"{scope}.{statement.name}"] = _reads_caddy(statement)
        elif _is_test_class(statement):
            found.update(_tests(f"{scope}.{statement.name}", [*statement.decorator_list, *statement.body]))
        else:
            reads = reads or _reads_caddy(statement)
    return {scope: reads, **{test: needs or reads for test, needs in found.items()}}


def _reads_outside_tests(statements):
    """Whether a module's or a test class's statements read WEBSPEC_TEST_CADDY outside its tests."""
    return any(_reads_outside_tests([*s.decorator_list, *s.body]) if _is_test_class(s)
               else not _is_test(s) and _reads_caddy(s) for s in statements)


def _imported(tree):
    """The last part of the name of each module that tree imports or imports from, and of each
    name it imports: test_caddy_limits for `from tests.test_caddy_limits import served`, `from .
    import test_caddy_limits` and `import tests.test_caddy_limits`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield (node.module or "").rpartition(".")[2]
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield from (alias.name.rpartition(".")[2] for alias in node.names)


def gateway_tests(files):
    """{module: {id: whether it needs WEBSPEC_TEST_CADDY}} for each test module that the job
    collects, with its test classes and its tests."""
    return {module: _tests(module, tree.body) for module, tree, is_test in files if is_test}


def test_the_gate_requires_every_test_that_needs_the_real_caddy(jobs, files):
    """A test that needs the real Caddy skips without it, and unless a name that the gate requires
    is in its id, it can stop running while CI stays green, as all of test_caddy_limits.py's could.
    A module or class that reads WEBSPEC_TEST_CADDY outside its tests needs that name in its own
    id, as it can skip whole and is then reported alone. And each name the gate requires is in the
    id of a test that needs the real Caddy, so that a rename, or a test that no longer reads
    WEBSPEC_TEST_CADDY by that name, fails here before the suite runs."""
    _, _, names, _ = gate(jobs["pytest"]["steps"])
    needs = sorted(test for ids in gateway_tests(files).values() for test, need in ids.items() if need)
    assert "tests.test_caddy_limits" in needs  # a module that reads it outside its tests is found
    assert [test for test in needs if not any(name in test for name in names)] == [], \
        f"none of the gate's --ran names {names} is in these ids"
    assert [name for name in names if not any(name in test for test in needs)] == []


def test_the_gate_requires_each_module_of_them_by_a_name_of_its_own(jobs, files):
    """require-tests.py fails when it finds no test by a name that it requires. A module whose
    tests drop out of the run (a rename, --ignore, --deselect, -k, collect_ignore) is not in the
    report, and one that skips at collection is in it by its own id alone; a name that is in
    another module's ids too, as test_real_caddy is, still finds that module's tests and passes.
    So each module with a test that needs the real Caddy is required by a name that is in some id
    of that module and in no id of another."""
    _, _, names, _ = gate(jobs["pytest"]["steps"])
    tests = gateway_tests(files)

    def its_own(name, module):
        elsewhere = (test for other, ids in tests.items() if other != module for test in ids)
        return any(name in test for test in tests[module]) and not any(name in test for test in elsewhere)

    unnamed = [module for module, ids in tests.items()
               if any(ids.values()) and not any(its_own(name, module) for name in names)]
    assert unnamed == [], f"none of the gate's --ran names {names} is these modules' own"


def test_only_the_tests_that_need_the_real_caddy_read_webspec_test_caddy(files):
    """The tests above find a test that needs the real Caddy where it reads WEBSPEC_TEST_CADDY: in
    the test, or in its module or test class. A read anywhere else reaches tests that they cannot
    name: one in conftest.py (a fixture), a helper or the gateway's own code, or one outside the
    tests of a test module that another file imports from (its CADDY, its fixtures)."""
    elsewhere = [module for module, tree, is_test in files if not is_test and _reads_caddy(tree)]
    assert elsewhere == [], "these read WEBSPEC_TEST_CADDY and are not test modules"
    shared = {module.rpartition(".")[2]: module for module, tree, is_test in files
              if is_test and _reads_outside_tests(tree.body)}
    importers = sorted({(module, shared[name]) for module, tree, _ in files
                        for name in _imported(tree) if name in shared and shared[name] != module})
    assert importers == [], "these import from a test module that reads WEBSPEC_TEST_CADDY outside its tests"


def test_the_suite_runs_on_every_python_from_the_oldest_the_gateway_admits_to_3_13(jobs):
    """3.13 is Debian 13's python3, which the Linux installer builds with by default, and its
    subprocess starts children with posix_spawn where 3.11 and 3.12 fork and exec. Not every
    difference shows on the hosted runner: glibc's posix_spawn leaves signals 32 and 33 ignored in
    the child only where seccomp refuses clone3, as Docker's default profile does: P19's test of the
    Docker init failed on 3.13 in such a container, and passed on the runner's VM, which has no
    seccomp filter, until the test started the init by fork and exec."""
    versions = jobs["pytest"]["strategy"]["matrix"]["python-version"]
    assert all(isinstance(v, str) for v in versions)  # YAML reads 3.10 as 3.1
    admitted = tomllib.loads((GATEWAY / "pyproject.toml").read_text())["project"]["requires-python"]
    oldest = re.fullmatch(r">=\s*3\.(\d+)", admitted.strip())
    assert oldest, f"requires-python = {admitted!r}: read its oldest Python here"
    minors = sorted(int(m.group(1)) for v in versions if (m := re.fullmatch(r"3\.(\d+)", v)))
    assert minors == list(range(int(oldest.group(1)), minors[-1] + 1))  # from the oldest, none skipped
    assert minors[-1] >= 13
