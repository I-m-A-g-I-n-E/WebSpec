"""Turn each rule ID defined in docs/spec (**MB-1**) into a badge that links to itself (#mb-1).

docs/tests/test_docs_consistency.py guarantees that every bold rule ID in docs/spec is a unique
definition, so each one can own an anchor.
"""

import re

RULE = re.compile(r"<strong>([A-Z]{2})-(\d+)</strong>")


def _badge(match: re.Match) -> str:
    rule = f"{match[1]}-{match[2]}"
    anchor = rule.lower()
    return f'<strong class="ws-rule" id="{anchor}"><a href="#{anchor}" title="Link to {rule}">{rule}</a></strong>'


def on_page_content(html, page, **kwargs):
    if not page.file.src_uri.startswith("spec/"):
        return html
    return RULE.sub(_badge, html)
