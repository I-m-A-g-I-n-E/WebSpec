from webspec_registry.records import ToolRecord, AccountRecord


def test_tool_record_fields():
    r = ToolRecord(service="mail-proton", tool="send_email", description="Send an email",
                   verb="send", noun="message", tier="sensitive", input_schema={"type": "object"})
    assert r.service == "mail-proton" and r.verb == "send" and r.noun == "message"
    assert r.tier == "sensitive" and r.input_schema["type"] == "object"


def test_account_record_defaults():
    a = AccountRecord(vault="Personal", item="OpenAI", title="OpenAI", category="API_CREDENTIAL")
    assert a.urls == () and a.fields_present == () and a.linked_service is None
