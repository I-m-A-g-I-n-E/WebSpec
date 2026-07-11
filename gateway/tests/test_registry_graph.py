from webspec_registry.graph import build_graph, to_dot, poset
from webspec_registry.records import ToolRecord, AccountRecord

CATALOG = [
    ToolRecord("slack", "send_message", "Send a message", "send", "message", "sensitive"),
    ToolRecord("op-auth", "run", "Run op", "invoke", "run", "dangerous"),
]
ACCOUNTS = [AccountRecord("Personal", "i1", "Slack", "LOGIN", linked_service="slack")]


def test_graph_has_service_noun_verb_nodes():
    g = build_graph(CATALOG, ACCOUNTS)
    ids = {n["id"] for n in g.nodes}
    assert "service:slack" in ids and "noun:message" in ids and "verb:send" in ids
    assert "account:Personal/i1" in ids


def test_graph_links_account_to_service():
    g = build_graph(CATALOG, ACCOUNTS)
    assert any(e["src"] == "account:Personal/i1" and e["dst"] == "service:slack"
               and e["rel"] == "credentials" for e in g.edges)


def test_to_dot_renders():
    dot = to_dot(build_graph(CATALOG))
    assert dot.startswith("digraph webspec {")
    assert "->" in dot and dot.rstrip().endswith("}")


def test_poset_ranks_by_tier():
    p = poset(CATALOG)
    assert p[("send", "message")] == 1   # sensitive
    assert p[("invoke", "run")] == 2     # dangerous
