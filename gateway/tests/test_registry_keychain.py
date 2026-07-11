import pytest
from webspec_registry.keychain import read_accounts


class FakeOp:
    """Mimics op-auth. read()/get_item() raise if called — they must never be."""
    def __init__(self):
        self.read_called = False

    def list_vaults(self):
        return [{"id": "v1", "name": "Personal"}]

    def list_items(self, vault):
        return [{
            "id": "i1", "title": "OpenAI", "category": "API_CREDENTIAL",
            "urls": [{"href": "https://platform.openai.com"}],
            "updated_at": "2026-01-01T00:00:00Z",
            # a value MUST NOT appear in output even if present here
            "fields": [{"label": "api_key", "value": "sk-SECRET-123"}],
        }]

    def read(self, *a, **k):
        self.read_called = True
        raise AssertionError("read() must never be called by the keychain reader")

    def get_item(self, *a, **k):
        raise AssertionError("get_item() must never be called by the keychain reader")


def test_reads_metadata_into_account_records():
    op = FakeOp()
    accounts = read_accounts(op)
    assert len(accounts) == 1
    a = accounts[0]
    assert a.vault == "Personal" and a.title == "OpenAI"
    assert a.category == "API_CREDENTIAL"
    assert a.urls == ("https://platform.openai.com",)


def test_never_surfaces_secret_values():
    op = FakeOp()
    accounts = read_accounts(op)
    blob = repr(accounts)
    assert "sk-SECRET-123" not in blob
    assert op.read_called is False
