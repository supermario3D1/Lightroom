"""Decision layer: genre recipes, intents, profiles, explainability, safety."""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from kestrel.decision import recipes
from kestrel.models import GlobalAdjust, ProfileSpec
from kestrel.profiles.library import default_profile_for

GENRES = ["sports", "wedding", "portrait", "event", "landscape", "wildlife",
          "commercial", "general"]


def _analysis_with(base, genre=None):
    a = copy.deepcopy(base)
    if genre:
        a.genre = genre
    return a


def _plan(base, genre="sports", intent="automatic", strength=1.0, profile=None,
          quality="high", manual=None):
    a = _analysis_with(base, genre)
    spec = profile or default_profile_for(genre)
    return recipes.build_recipe(a, spec, intent, strength, quality, manual=manual)


def test_every_genre_produces_bounded_settings(analysis):
    base = analysis[0]
    for genre in GENRES:
        r = _plan(base, genre)
        g = r.global_
        assert -3.0 <= g.exposure <= 3.0, (genre, g.exposure)
        for field in ("contrast", "highlights", "shadows", "whites", "blacks", "texture",
                      "clarity", "dehaze", "vibrance", "saturation"):
            value = getattr(g, field)
            assert -100.0 <= value <= 100.0, (genre, field, value)
        assert 0.0 <= g.sharpness <= 150.0, (genre, g.sharpness)
        assert 0.0 <= g.luminance_nr <= 100.0
        assert 0.0 <= g.color_nr <= 100.0
        if g.temperature is not None:
            assert 2000 <= g.temperature <= 12000, (genre, g.temperature)
            assert -100 <= (g.tint or 0) <= 100


def test_sports_recipe_is_contrasty_but_controlled(analysis):
    r = _plan(analysis[0], "sports")
    g = r.global_
    assert g.contrast > 0
    assert g.highlights < 0, "highlights are held back for kit and faces"
    assert g.shadows > 0, "shadows are opened"
    assert g.vibrance >= 0
    names = {m.kind for m in r.masks}
    assert "subject" in names and "background" in names
    assert len(r.masks) >= 3


def test_wedding_and_portrait_are_gentaler_than_sports(analysis):
    base = analysis[0]
    sports = _plan(base, "sports").global_
    wedding = _plan(base, "wedding").global_
    portrait = _plan(base, "portrait").global_
    assert abs(wedding.contrast) < abs(sports.contrast)
    assert abs(portrait.clarity) <= abs(sports.clarity) + 1


def test_every_plan_explains_itself(analysis):
    base = analysis[0]
    for genre in GENRES:
        r = _plan(base, genre)
        assert r.explanation, genre
        for item in r.explanation:
            assert item.area and item.action and item.reason, (genre, item)
            assert 0.0 <= item.confidence <= 1.0


def test_intents_change_the_rendering_not_the_safety_limits(analysis):
    base = analysis[0]
    natural = _plan(base, "sports", intent="natural").global_
    dramatic = _plan(base, "sports", intent="dramatic").global_
    assert abs(dramatic.contrast) >= abs(natural.contrast)
    assert dramatic.exposure <= 1.0 and dramatic.exposure >= -1.0
    assert dramatic.saturation <= 30


def test_strength_scales_the_edit(analysis):
    base = analysis[0]
    full = _plan(base, "sports", strength=1.0).global_
    half = _plan(base, "sports", strength=0.5).global_
    assert abs(half.exposure) <= abs(full.exposure) + 0.01
    assert abs(half.contrast) <= abs(full.contrast) + 0.01
    assert abs(half.clarity) <= abs(full.clarity) + 0.01


def test_clipped_capture_gets_highlight_protection(analysis):
    base = _analysis_with(analysis[0], "sports")
    base.exposure.clipping_high = 0.08
    base.exposure.headroom_high = 0.1
    r = recipes.build_recipe(base, default_profile_for("sports"), "automatic", 1.0, "high")
    assert r.global_.highlights < 0
    assert r.global_.whites <= 10


def test_flat_capture_gets_more_contrast_than_contrasty_one(analysis):
    flat = _analysis_with(analysis[0], "sports")
    flat.exposure.p05 = 0.30
    flat.exposure.p95 = 0.62
    punchy = _analysis_with(analysis[0], "sports")
    punchy.exposure.p05 = 0.02
    punchy.exposure.p95 = 0.94
    flat_plan = recipes.build_recipe(flat, default_profile_for("sports"), "automatic", 1.0, "high")
    punchy_plan = recipes.build_recipe(punchy, default_profile_for("sports"), "automatic", 1.0,
                                       "high")
    assert flat_plan.global_.contrast > punchy_plan.global_.contrast


def test_noisy_capture_gets_noise_reduction(analysis):
    noisy = _analysis_with(analysis[0], "sports")
    noisy.quality.noise_sigma = 9.0
    noisy.quality.iso_estimate = 6400
    r = recipes.build_recipe(noisy, default_profile_for("sports"), "automatic", 1.0, "high")
    clean = recipes.build_recipe(analysis[0], default_profile_for("sports"), "automatic", 1.0, "high")
    assert r.global_.luminance_nr >= clean.global_.luminance_nr
    assert r.global_.sharpness <= clean.global_.sharpness + 6


def test_recipe_is_deterministic(analysis):
    base = analysis[0]
    a = recipes.build_recipe(base, default_profile_for("sports"), "automatic", 1.0, "high")
    b = recipes.build_recipe(base, default_profile_for("sports"), "automatic", 1.0, "high")
    assert json.dumps([e.to_dict() for e in a.explanation], sort_keys=True) == \
        json.dumps([e.to_dict() for e in b.explanation], sort_keys=True)
    assert a.global_.to_dict() == b.global_.to_dict()


def test_profile_advanced_hooks_are_honoured(analysis):
    base = analysis[0]
    plain = recipes.build_recipe(base, default_profile_for("sports"), "automatic", 1.0, "high")
    tuned = ProfileSpec(name="Test", genre="sports", advanced={
        "temp_bias_k": 600, "clarity_bias": 6, "grass_boost": 1.2, "crowd_control": 1.5,
    })
    adjusted = recipes.build_recipe(base, tuned, "automatic", 1.0, "high")
    if plain.global_.temperature is not None and adjusted.global_.temperature is not None:
        assert adjusted.global_.temperature >= plain.global_.temperature
    assert adjusted.global_.clarity >= plain.global_.clarity


def test_manual_overrides_win(engine, imported, analysis):
    a, masks, _ = analysis
    plan, _, _ = engine.build_plan(imported, manual={"exposure": 0.75, "saturation": -5},
                                   analysis=a, masks=masks)
    assert abs(plan.global_.exposure - 0.75) < 1e-6
    assert abs(plan.global_.saturation - (-5)) < 1e-6
    # out-of-range overrides are clamped, never written through
    clamped, _, _ = engine.build_plan(imported, manual={"exposure": 42, "temperature": 900},
                                      analysis=a, masks=masks)
    assert clamped.global_.exposure == 5.0
    assert clamped.global_.temperature == 2000


def test_skin_adjustments_are_conservative(analysis):
    r = _plan(analysis[0], "sports")
    skin_layers = [m for m in r.masks if m.kind in ("skin", "face")]
    for layer in skin_layers:
        assert abs(layer.adjustments.saturation) <= 12
        assert abs(layer.adjustments.exposure) <= 0.5
        assert layer.adjustments.temperature is None or 4300 <= layer.adjustments.temperature <= 7600


def test_masks_carry_confidence_and_notes(analysis):
    r = _plan(analysis[0], "sports")
    for layer in r.masks:
        assert 0.0 <= layer.confidence <= 1.0
        assert layer.notes, layer.name
        assert layer.feather >= 0.0


def test_unknown_genre_falls_back_to_general(analysis):
    base = analysis[0]
    weird = _analysis_with(base, "underwater_polo")
    r = recipes.build_recipe(weird, default_profile_for("general"), "automatic", 1.0, "high")
    g = r.global_
    assert abs(g.exposure) <= 3.0 and abs(g.contrast) <= 100


def test_crop_suggestion_is_sane(analysis):
    crop = recipes.suggest_crop(analysis[0])
    assert isinstance(crop, dict)
    assert "available" in crop
    if crop.get("available"):
        assert crop.get("kind") in ("rule-of-thirds", "centred", "horizon")
        assert "suggestion" in crop
        assert len(crop.get("subject") or []) in (0, 2)
