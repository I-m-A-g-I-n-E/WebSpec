from webspec.guard import build_provenance_link, validate_provenance_chain

def test_single_link_human(session_key):
    link = build_provenance_link(session_key, "human", "agent", "read")
    chain = f"human:{link}"
    result = validate_provenance_chain(session_key, chain, "read")
    assert result.valid is True
    assert result.origin == "human"
    assert len(result.links) == 1

def test_two_link_chain(session_key):
    link1 = build_provenance_link(session_key, "human", "agent", "read")
    link2 = build_provenance_link(session_key, "agent", "gateway", "read")
    chain = f"human:{link1}->agent:{link2}"
    result = validate_provenance_chain(session_key, chain, "read")
    assert result.valid is True
    assert result.origin == "human"
    assert len(result.links) == 2

def test_broken_chain_bad_sig(session_key):
    chain = "human:deadbeef"
    result = validate_provenance_chain(session_key, chain, "read")
    assert result.valid is False

def test_missing_chain(session_key):
    result = validate_provenance_chain(session_key, None, "read")
    assert result.valid is False
    assert result.origin == "unknown"

def test_empty_chain(session_key):
    result = validate_provenance_chain(session_key, "", "read")
    assert result.valid is False
