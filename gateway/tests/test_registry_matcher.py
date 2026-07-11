from webspec_registry.matcher import LexicalMatcher
from webspec_registry.records import ToolRecord

SEND = ToolRecord("slack", "send_message", "Send a message to a channel", "send", "message", "sensitive")
READ = ToolRecord("gdrive", "get_file", "Download a file", "read", "file", "open")


def test_relevant_scores_higher_than_irrelevant():
    m = LexicalMatcher()
    assert m.score("send a message", SEND) > m.score("send a message", READ)


def test_verb_noun_bonus():
    m = LexicalMatcher()
    # canonical verb+noun match should push score above raw token overlap alone
    assert m.score("fire off a note", SEND) > 0.0


def test_empty_query_is_zero():
    assert LexicalMatcher().score("", SEND) == 0.0
