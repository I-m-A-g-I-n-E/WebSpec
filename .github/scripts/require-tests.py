#!/usr/bin/env python3
"""Fail CI when tests it relies on did not run.

pytest counts a skipped test as a success, so a test that needs something CI provides (the
real Caddy behind WEBSPEC_TEST_CADDY, Markdown for the docs rendering tests) can stop running
without turning CI red. This reads the JUnit XML report that `pytest --junitxml=REPORT` writes.

Usage: require-tests.py REPORT [--ran NAME]... [--no-skips]

  --ran NAME   at least one test whose id ("tests.test_caddy.test_real_caddy_...") contains
               NAME ran, and every such test passed
  --no-skips   no test was skipped
"""
import argparse
import os
import sys
import xml.etree.ElementTree as ET


def _outcome(case):
    for tag in ("failure", "error", "skipped"):
        element = case.find(tag)
        if element is not None:
            # A skip's text holds its location and reason ("collection skipped" is its message
            # when a whole module skips); a failure's text is the traceback.
            text = (element.text or "").strip() if tag == "skipped" else ""
            return tag, text or element.get("message", "")
    return "passed", ""


def _test_id(case):
    return ".".join(part for part in (case.get("classname"), case.get("name")) if part)


def check(report, ran=(), no_skips=False):
    """(problems, summary): why REPORT fails the requirements (none if it meets them), and a summary."""
    cases = [(_test_id(case), *_outcome(case)) for case in ET.parse(report).getroot().iter("testcase")]
    problems, summary = [], []
    for name in ran:
        matching = [case for case in cases if name in case[0]]
        if not matching:
            problems.append(f"no test matching {name!r} ran")
        problems += [f"{test_id} {outcome}: {message}"
                     for test_id, outcome, message in matching if outcome != "passed"]
        summary.append(f"{len(matching)} test(s) matching {name!r}")
    skipped = [(test_id, message) for test_id, outcome, message in cases if outcome == "skipped"]
    if no_skips:
        problems += [f"{test_id} skipped: {message}" for test_id, message in skipped]
    summary.append(f"{len(cases)} test(s) in the report, {len(skipped)} skipped")
    return list(dict.fromkeys(problems)), summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", help="JUnit XML from pytest --junitxml")
    parser.add_argument("--ran", action="append", default=[], metavar="NAME",
                        help="tests whose id contains NAME must have run and passed")
    parser.add_argument("--no-skips", action="store_true", help="no test may be skipped")
    args = parser.parse_args(argv)
    problems, summary = check(args.report, args.ran, args.no_skips)
    for problem in problems:
        if os.environ.get("GITHUB_ACTIONS") == "true":  # an annotation on the run and the PR
            escaped = problem.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error title=Required tests::{escaped}")
        else:
            print(f"require-tests: {problem}", file=sys.stderr)
    print(f"require-tests: {'; '.join(summary)}: {'FAILED' if problems else 'ok'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
