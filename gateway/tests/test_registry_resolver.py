from webspec_registry.resolver import resolve
from webspec_registry.records import ToolRecord

CATALOG = [
    ToolRecord("slack", "send_message", "Send a message", "send", "message", "sensitive"),
    ToolRecord("email", "send_email", "Send an email", "send", "message", "sensitive"),
    ToolRecord("gdrive", "get_file", "Download a file", "read", "file", "open"),
]


def test_ranks_relevant_tool_first():
    results = resolve("fire off a note", CATALOG)
    assert results[0].record.noun == "message"
    assert results[0].record.service in {"slack", "email"}


def test_status_weight_orders_connected_over_keychain():
    def status_of(rec):
        return "connected" if rec.service == "email" else "keychain"
    results = resolve("send a message", CATALOG, status_of=status_of)
    assert results[0].record.service == "email"  # connected (1.0) beats keychain (0.8)


def test_unavailable_filtered_out():
    results = resolve("send a message", CATALOG, status_of=lambda r: "unavailable")
    assert results == []
