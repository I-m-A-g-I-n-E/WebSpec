"""Verb/noun normalization and sensitivity tier classification."""
from pathlib import Path

import pytest

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
    pytest.importorskip("yaml")
    # FABRICATE and SYNTHESIZE are rotation_candidates under POST/CREATE in
    # docs/http-methods/verb-atlas.yaml and are NOT present in the seed
    # VERB_CANON, so they can only resolve via atlas enrichment.
    assert normalize("fabricate_report")[0] == "create"
    assert normalize("synthesize_data")[0] == "create"


def test_atlas_enriches_amend_family_rotation_candidate():
    pytest.importorskip("yaml")
    # CORRECT is a rotation_candidate under PATCH/AMEND in the atlas and is
    # not present in the seed VERB_CANON.
    assert normalize("correct_typo")[0] == "amend"


def test_atlas_loader_returns_entries_when_atlas_present():
    pytest.importorskip("yaml")
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


# --- Atlas loading must be TOTAL: never raise, even on PermissionError -----
#
# Deterministic (no chmod, which is CI-flaky across filesystems/containers):
# monkeypatch the resolution path to raise PermissionError directly, the way
# a hardened systemd sandbox / bad WEBSPEC_VERB_ATLAS / NFS hiccup would.


def test_load_atlas_verb_map_survives_permission_error_from_find_path(monkeypatch):
    import webspec_registry.normalize as normalize_mod

    def _raise(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(normalize_mod, "_find_atlas_path", _raise)
    # The outer try/except Exception in _load_atlas_verb_map must absorb
    # this -- optional enrichment failing must NEVER raise. This is exactly
    # what runs at module IMPORT time to build _EFFECTIVE_VERB_CANON, so a
    # raise here would be an import-time crash of the whole registry.
    assert normalize_mod._load_atlas_verb_map() == {}
    # And normalize() must still work via the seed-only map.
    assert normalize_mod.normalize("send_email") == ("send", "message")


def test_find_atlas_path_survives_permission_error_from_exists(monkeypatch):
    # pathlib.Path.exists() only swallows ENOENT/ENOTDIR/EBADF/ELOOP
    # internally -- NOT PermissionError (EACCES). Simulate a
    # permission-denied path (e.g. a chmod-000 ancestor directory under a
    # hardened systemd sandbox) by making Path.exists raise directly, and
    # assert _find_atlas_path degrades to None instead of propagating.
    import webspec_registry.normalize as normalize_mod

    def _raise_exists(self):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "exists", _raise_exists)
    monkeypatch.setenv("WEBSPEC_VERB_ATLAS", "/some/inaccessible/atlas.yaml")
    assert normalize_mod._find_atlas_path() is None
    assert normalize_mod._load_atlas_verb_map() == {}
    assert normalize_mod.normalize("send_email") == ("send", "message")


def test_import_survives_atlas_resolution_failure(monkeypatch):
    # _load_atlas_verb_map() is called at MODULE IMPORT TIME (to build
    # _EFFECTIVE_VERB_CANON), and normalize.py is imported at module scope
    # by catalog.py and __main__.py. Prove that calling the loader -- as
    # import does -- never lets a PermissionError escape, by simulating the
    # failure mode and re-invoking exactly the call import performs.
    import webspec_registry.normalize as normalize_mod

    def _raise(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(normalize_mod, "_find_atlas_path", _raise)
    result = normalize_mod._load_atlas_verb_map()  # what import-time does
    assert result == {}
