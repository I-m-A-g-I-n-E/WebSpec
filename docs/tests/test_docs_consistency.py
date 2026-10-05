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
