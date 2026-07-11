"""Guard against the spec drifting from the implementation again."""
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1]


def test_status_matrix_present():
    idx = (DOCS / "index.md").read_text()
    assert "Implemented" in idx and "Proposed" in idx


def test_discovery_does_not_use_banned_provider_suffix():
    # url-grammar bans `object.provider`; discovery must not demonstrate it.
    for md in (DOCS / "discovery").glob("*.md"):
        text = md.read_text()
        for banned in ("/message.slack", "/file.gdrive", "/spreadsheet.gsheets"):
            assert banned not in text, f"{md.name} still uses banned form {banned}"


def test_roadmap_c_exists():
    assert (DOCS / "ROADMAP-C.md").exists()


def test_no_banned_provider_suffix_repo_wide():
    # Regression guard: the `object.provider` path form is banned by url-grammar
    # (see core-syntax.md's "Design Principle: No Redundancy"). Track A fixed
    # docs/discovery/*; this guards the banned form from silently reappearing
    # anywhere under docs/, except the files that intentionally show it as a
    # counter-example, and design docs under superpowers/.
    allowlist = {
        DOCS / "url-grammar" / "core-syntax.md",
        DOCS / "url-grammar" / "complete-grammar-ebnf.md",
    }
    banned = (
        "/message.slack",
        "/file.gdrive",
        "/spreadsheet.gsheets",
        "/message.email",
        "/message.sms",
        "/message.teams",
        "/file.slack",
    )
    for md in DOCS.rglob("*.md"):
        rel_parts = md.relative_to(DOCS).parts
        if "superpowers" in rel_parts or ".pytest_cache" in rel_parts:
            continue
        if md in allowlist:
            continue
        text = md.read_text()
        for term in banned:
            assert term not in text, (
                f"{md.relative_to(DOCS)} still uses banned form {term!r} "
                "(object.provider path is banned by url-grammar/core-syntax.md)"
            )
