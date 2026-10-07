"""require-tests.py fails CI when a test that CI relies on did not run, which pytest alone passes.

pytest counts a skipped test as a success, so the real-Caddy tests (WEBSPEC_TEST_CADDY) and the
docs rendering tests (Markdown, pymdown-extensions) could stop running while CI stays green; the
jobs run require-tests.py on pytest's JUnit report to catch that. The reports here are written by
pytest itself, from small test modules, so a change in how pytest reports a skip is caught too.

Run from the repository root: python -m pytest -q -p no:cacheprovider .github/scripts
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().with_name("require-tests.py")

PASSES = """
    def test_real_caddy_validates():
        pass
"""

SKIPS = """
    import pytest

    @pytest.mark.skipif(True, reason="set WEBSPEC_TEST_CADDY to a caddy binary")
    def test_real_caddy_validates():
        pass
"""

# As test_rendering.py does without Markdown: the whole module skips, at collection.
MODULE_SKIPS = """
    import pytest

    pytest.importorskip("no_such_module_for_require_tests")

    def test_rule_is_its_own_list_item():
        pass
"""

FAILS = """
    def test_real_caddy_validates():
        assert False, "caddy validate failed"
"""

ERRS = """
    import pytest

    @pytest.fixture
    def caddy():
        raise RuntimeError("no caddy")

    def test_real_caddy_validates(caddy):
        pass
"""


def report(tmp_path, **modules) -> Path:
    """The JUnit report of a pytest run over these test modules ({name: source})."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "pytest.ini").write_text("[pytest]\n")  # this run's own rootdir and settings
    for name, source in modules.items():
        (tests / f"{name}.py").write_text(textwrap.dedent(source))
    xml = tmp_path / "report.xml"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"],
                         cwd=tests, env=env, capture_output=True, encoding="utf-8", timeout=120)
    assert xml.exists(), run.stdout + run.stderr
    return xml


def require(xml, *args, github=False):
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
    if github:
        env["GITHUB_ACTIONS"] = "true"
    return subprocess.run([sys.executable, str(SCRIPT), str(xml), *args], env=env, capture_output=True,
                          encoding="utf-8", timeout=60)


def test_a_required_test_that_passed(tmp_path):
    r = require(report(tmp_path, test_caddy=PASSES), "--ran", "test_real_caddy")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 test(s) matching 'test_real_caddy'" in r.stdout
    assert r.stdout.rstrip().endswith("ok")


def test_no_test_by_that_name(tmp_path):
    r = require(report(tmp_path, test_caddy="def test_something_else():\n    pass\n"), "--ran", "test_real_caddy")
    assert r.returncode == 1
    assert "no test matching 'test_real_caddy' ran" in r.stderr
    assert r.stdout.rstrip().endswith("FAILED")


@pytest.mark.parametrize("args", [["--ran", "test_real_caddy"], ["--no-skips"]])
def test_a_skipped_test(tmp_path, args):
    r = require(report(tmp_path, test_caddy=SKIPS), *args)
    assert r.returncode == 1
    assert "test_caddy.test_real_caddy_validates skipped" in r.stderr
    assert "set WEBSPEC_TEST_CADDY to a caddy binary" in r.stderr  # and why it skipped


@pytest.mark.parametrize("args", [["--ran", "test_rendering"], ["--no-skips"]])
def test_a_module_that_skips_whole(tmp_path, args):
    """pytest reports one skipped entry for the module, named after it, and no test in it."""
    r = require(report(tmp_path, test_rendering=MODULE_SKIPS), *args)
    assert r.returncode == 1
    assert "test_rendering skipped" in r.stderr
    assert "no_such_module_for_require_tests" in r.stderr


@pytest.mark.parametrize("source, outcome", [(FAILS, "failure"), (ERRS, "error")], ids=["failed", "error"])
def test_a_required_test_that_did_not_pass(tmp_path, source, outcome):
    r = require(report(tmp_path, test_caddy=source), "--ran", "test_real_caddy")
    assert r.returncode == 1
    assert f"test_caddy.test_real_caddy_validates {outcome}" in r.stderr


def test_other_tests_may_skip_unless_no_skips(tmp_path):
    xml = report(tmp_path, test_caddy=PASSES, test_other=SKIPS.replace("test_real_caddy", "test_optional"))
    assert require(xml, "--ran", "test_real_caddy").returncode == 0
    r = require(xml, "--ran", "test_real_caddy", "--no-skips")
    assert r.returncode == 1
    assert "test_other.test_optional_validates skipped" in r.stderr


def test_on_github_each_problem_is_one_error_annotation(tmp_path):
    source = """
        import pytest

        def test_real_caddy_validates():
            pytest.skip("100% skipped\\nfor a reason on two lines")
    """
    r = require(report(tmp_path, test_caddy=source), "--ran", "test_real_caddy", "--ran", "test_missing",
                github=True)
    assert r.returncode == 1
    errors = [line for line in r.stdout.splitlines() if line.startswith("::error title=Required tests::")]
    assert len(errors) == 2, r.stdout
    assert "100%25 skipped%0Afor a reason on two lines" in errors[0]  # escaped, so it stays one line
    assert errors[1].endswith("no test matching 'test_missing' ran")
    assert r.stderr == ""


@pytest.mark.parametrize("content", [None, "<testsuites><testsuite>"], ids=["missing", "truncated"])
def test_a_report_it_cannot_read_fails(tmp_path, content):
    xml = tmp_path / "report.xml"
    if content is not None:
        xml.write_text(content)
    assert require(xml, "--ran", "test_real_caddy").returncode != 0
