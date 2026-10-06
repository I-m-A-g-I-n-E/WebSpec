"""Every rule must render as its own list item (a missing blank line can glue it into the text above)."""
import re
from pathlib import Path

import pytest

markdown = pytest.importorskip("markdown")
pytest.importorskip("pymdownx")

SPEC = Path(__file__).resolve().parents[1] / "spec"
EXTENSIONS = ["tables", "admonition", "pymdownx.superfences", "pymdownx.details", "attr_list", "def_list"]


@pytest.mark.parametrize("page", sorted(SPEC.glob("*.md")), ids=lambda p: p.name)
def test_every_rule_starts_its_own_list_item(page):
    source = page.read_text()
    html = markdown.markdown(source, extensions=EXTENSIONS)
    rules = re.findall(r"\*\*([A-Z]{2}-\d+)\*\*", source)
    for rule in rules:
        assert re.search(rf"<li>\s*(<p>)?\s*<strong>{rule}</strong>", html), f"{rule} does not start a list item"
