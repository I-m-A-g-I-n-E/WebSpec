"""Guard the spec against drifting from itself, from the site config, and from the code."""
import re
from pathlib import Path

import yaml

DOCS = Path(__file__).resolve().parents[1]
REPO = DOCS.parent
SPEC = DOCS / "spec"

RULE_DEF = re.compile(r"\*\*([A-Z]{2})-(\d+)\*\*")


def _pages():
    return [p for p in DOCS.rglob("*.md") if "tests" not in p.relative_to(DOCS).parts]


def _rule_definitions():
    defs = []
    for md in sorted(SPEC.glob("*.md")):
        defs += [(prefix, int(n), md.name) for prefix, n in RULE_DEF.findall(md.read_text())]
    return defs


def test_status_matrix_present():
    idx = (DOCS / "index.md").read_text()
    assert "Implemented" in idx and "Proposed" in idx


def test_rule_ids_are_unique_and_numbered_in_order():
    defs = _rule_definitions()
    assert defs, "no rule IDs found"
    seen = {}
    for prefix, n, page in defs:
        assert (prefix, n) not in seen, f"{prefix}-{n} defined twice ({seen[(prefix, n)]}, {page})"
        seen[(prefix, n)] = page
    for prefix in {p for p, _ in seen}:
        numbers = sorted(n for p, n in seen if p == prefix)
        assert numbers == list(range(1, len(numbers) + 1)), f"{prefix} rules are not numbered 1..n: {numbers}"


def test_every_rule_reference_resolves():
    defined = {(p, n) for p, n, _ in _rule_definitions()}
    prefixes = "|".join(sorted({p for p, _ in defined}))
    ref = re.compile(rf"\b({prefixes})-(\d+)\b")
    for md in _pages():
        for prefix, n in ref.findall(md.read_text()):
            assert (prefix, int(n)) in defined, f"{md.relative_to(DOCS)} cites undefined rule {prefix}-{n}"


def test_nav_lists_every_page_and_only_existing_pages():
    nav = yaml.safe_load((REPO / "mkdocs.yml").read_text())["nav"]

    def walk(items):
        for item in items:
            value = next(iter(item.values())) if isinstance(item, dict) else item
            if isinstance(value, list):
                yield from walk(value)
            else:
                yield value

    in_nav = set(walk(nav))
    on_disk = {str(p.relative_to(DOCS)) for p in _pages()}
    assert in_nav == on_disk, f"nav-only: {sorted(in_nav - on_disk)}; not in nav: {sorted(on_disk - in_nav)}"


def test_docs_paths_cited_in_code_exist():
    cited = re.compile(r"docs/[\w./-]+\.md")
    for py in (REPO / "gateway").rglob("*.py"):
        for path in cited.findall(py.read_text()):
            assert (REPO / path).exists(), f"{py.relative_to(REPO)} cites missing {path}"


def test_no_banned_provider_suffix():
    # The old `object.provider` path form (/message.slack) put the provider in the path as
    # well as the host. The destination label is the only provider slot; keep it that way.
    banned = re.compile(r"/\w+\.(slack|gdrive|email|sms|teams|gsheets|notion|discord|linear|gcal)\b")
    for md in _pages():
        match = banned.search(md.read_text())
        assert match is None, f"{md.relative_to(DOCS)} uses the banned object.provider form {match.group(0)!r}"
