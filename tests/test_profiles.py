"""Profiles: the 23 built-ins, validation, export/import, duplication."""
from __future__ import annotations

import json

import pytest

from kestrel.models import ProfileSpec
from kestrel.profiles import library as lib


@pytest.fixture(scope="module")
def builtins():
    return lib.builtin_profiles() if hasattr(lib, "builtin_profiles") else None


def test_builtin_catalogue_is_complete(engine):
    profiles = [p for p in engine.db.list_profiles() if p.builtin]
    assert len(profiles) == 23, len(profiles)
    ids = {p.id for p in profiles}
    assert len(ids) == 23, "profile ids must be unique"
    assert all(p.id.startswith("builtin:") for p in profiles)
    genres = {p.genre for p in profiles}
    assert {"sports", "wedding", "portrait", "event", "landscape", "wildlife",
            "commercial", "general"} <= genres


def test_sports_has_broad_coverage(engine):
    sports = [p for p in engine.db.list_profiles() if p.genre == "sports"]
    names = " ".join(p.name.lower() for p in sports)
    for sport in ("football", "basketball", "afl", "cricket", "rugby", "tennis",
                  "athletics", "swimming"):
        assert sport in names, sport


def test_profiles_describe_intent_not_sliders(engine):
    for profile in engine.db.list_profiles():
        assert profile.description
        assert 0.0 <= profile.subject_emphasis <= 1.5
        assert 0.0 <= profile.skin_protection <= 1.0
        assert profile.contrast_character in ("soft", "balanced", "punchy", "editorial")
        assert profile.colour_character in ("natural", "warm", "cool", "editorial", "vibrant")


def test_default_profile_matching_uses_scene_tags(engine):
    night = lib.default_profile_for("sports", ["stadium", "night"])
    indoor = lib.default_profile_for("sports", ["indoor"])
    generic = lib.default_profile_for("sports")
    assert night.id != generic.id
    assert indoor.id != generic.id
    assert lib.default_profile_for("sports", ["stadium", "night"]).id == night.id


def test_validation_rejects_nonsense():
    bad = ProfileSpec(name="", genre="sports", subject_emphasis=9.0)
    with pytest.raises(Exception):
        lib.validate_profile(bad)
    with pytest.raises(Exception):
        lib.validate_profile(ProfileSpec(name="x", genre="not-a-genre"))
    lib.validate_profile(ProfileSpec(name="Fine", genre="sports"))


def test_export_import_round_trip():
    spec = ProfileSpec(name="Night Football Custom", genre="sports",
                       description="tight subject separation",
                       advanced={"temp_bias_k": 250, "grass_boost": 1.1})
    document = lib.export_profile(spec)
    payload = json.loads(document)
    assert payload["format"] == "kestrel-profile"
    restored = lib.import_profile(document)
    assert restored.name == spec.name
    assert restored.genre == spec.genre
    assert restored.advanced["temp_bias_k"] == 250
    assert restored.builtin is False
    assert restored.id != spec.id or restored.id == "", "imports never reuse a builtin id"


def test_import_rejects_garbage():
    with pytest.raises(Exception):
        lib.import_profile("{not json")
    with pytest.raises(Exception):
        lib.import_profile(json.dumps({"format": "something-else"}))


def test_clone_profile_gets_a_new_identity():
    original = lib.default_profile_for("sports")
    clone = lib.clone_profile(original, "My Copy")
    assert clone.name == "My Copy"
    assert clone.builtin is False
    assert clone.id != original.id
    assert clone.genre == original.genre


def test_builtins_cannot_be_deleted(engine):
    builtin = [p for p in engine.db.list_profiles() if p.builtin][0]
    with pytest.raises(Exception):
        engine.db.delete_profile(builtin.id)


def test_custom_profile_lifecycle(engine):
    spec = ProfileSpec(name="Test Custom", genre="wedding", description="soft highlights")
    stored = engine.db.upsert_profile(spec)
    assert stored.id.startswith("prof_")
    fetched = engine.db.get_profile(stored.id)
    assert fetched.name == "Test Custom"
    engine.db.delete_profile(stored.id)
    with pytest.raises(Exception):
        engine.db.get_profile(stored.id)
