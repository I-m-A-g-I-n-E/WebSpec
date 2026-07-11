"""Verb/noun normalization and sensitivity tier classification."""
from webspec_registry.normalize import normalize, tier_of


def test_send_email():
    assert normalize("send_email") == ("send", "message")


def test_list_vaults_singularizes_noun():
    assert normalize("list_vaults") == ("read", "vault")


def test_colloquial_query_verb_and_noun():
    # "fire off a note" → send / message
    assert normalize("fire off a note") == ("send", "message")


def test_tier_opauth_known():
    assert tier_of("op-auth", "run", "invoke") == "dangerous"
    assert tier_of("op-auth", "read", "read") == "sensitive"
    assert tier_of("op-auth", "list_vaults", "read") == "open"


def test_tier_default_by_verb():
    assert tier_of("mail-proton", "send_email", "send") == "sensitive"
    assert tier_of("some-svc", "get_thing", "read") == "open"
