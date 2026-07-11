"""Verb/noun normalization and sensitivity tier classification."""
from webspec_registry.normalize import _load_atlas_verb_map, normalize, tier_of


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


# --- Verb Atlas enrichment (docs/http-methods/verb-atlas.yaml) -------------


def test_seed_wins_over_atlas_on_conflict():
    # Hard constraint: the hand-seeded VERB_CANON always wins over the atlas.
    # "send_email" already passes via the seed alone; this pins that the
    # merged lookup doesn't change it. ("email" is also a NOUN_CANON entry
    # mapping to "message", which is unrelated to the atlas merge but is the
    # actual current/expected behavior we must not regress.)
    assert normalize("send_email") == ("send", "message")


def test_atlas_enriches_create_family_rotation_candidates():
    # FABRICATE and SYNTHESIZE are rotation_candidates under POST/CREATE in
    # docs/http-methods/verb-atlas.yaml and are NOT present in the seed
    # VERB_CANON, so they can only resolve via atlas enrichment.
    assert normalize("fabricate_report")[0] == "create"
    assert normalize("synthesize_data")[0] == "create"


def test_atlas_enriches_amend_family_rotation_candidate():
    # CORRECT is a rotation_candidate under PATCH/AMEND in the atlas and is
    # not present in the seed VERB_CANON.
    assert normalize("correct_typo")[0] == "amend"


def test_atlas_loader_returns_entries_when_atlas_present():
    m = _load_atlas_verb_map()
    assert len(m) > 100
    assert m.get("fabricate") == "create"
    assert m.get("synthesize") == "create"
    assert m.get("correct") == "amend"


def test_atlas_loader_graceful_when_path_missing(monkeypatch):
    # Simulate the atlas being unavailable: point WEBSPEC_VERB_ATLAS at a
    # path that doesn't exist. Must not raise, must return {}.
    monkeypatch.setenv("WEBSPEC_VERB_ATLAS", "/nonexistent/does-not-exist.yaml")
    assert _load_atlas_verb_map() == {}


def test_normalize_seed_only_path_still_works_without_atlas():
    # Regardless of atlas availability, the seed-covered case must behave
    # identically -- this is the graceful-degradation invariant for
    # normalize() itself (module-level merge always keeps the seed intact).
    assert normalize("send_email") == ("send", "message")
