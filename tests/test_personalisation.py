"""Personalisation: opt-in, bounded, and never a silent retrain."""
from __future__ import annotations

import json

import pytest

from kestrel.models import EditPlan, GlobalAdjust, ProfileSpec
from kestrel.personalisation.learning import MAX_INFLUENCE, Personalisation
from kestrel.profiles.library import default_profile_for


@pytest.fixture()
def learner(engine, settings):
    settings.personalised_learning = False
    learning = Personalisation(engine.db, settings)
    learning.reset()
    yield learning
    learning.reset()
    settings.personalised_learning = False


def test_learning_is_off_by_default(learner):
    assert learner.enabled is False
    event = learner.record("accept", "sports", rating=5)
    assert event is None, "no model update without consent"
    assert learner.model == {}


def test_model_stays_empty_without_consent(engine, settings):
    learning = Personalisation(engine.db, settings)
    learning.reset()
    assert learning.summary()["enabled"] is False
    assert "opt-in" in learning.summary()["consent"]


def test_consent_then_learning_changes_a_plan(learner, settings):
    settings.personalised_learning = True
    for _ in range(6):
        learner.record("manual_adjust", "sports", manual_delta={"contrast": 12, "exposure": 0.2})
    pref = learner.model.get("sports")
    assert pref is not None and pref.samples >= 3
    plan = EditPlan(genre="sports", global_=GlobalAdjust(contrast=10, exposure=0.2))
    applied = learner.apply(plan, "sports", default_profile_for("sports"))
    assert applied.get("applied"), applied
    assert plan.global_.contrast != 10


def test_influence_is_bounded(learner, settings):
    settings.personalised_learning = True
    for _ in range(80):
        learner.record("manual_adjust", "sports", manual_delta={"contrast": 60, "exposure": 2.0})
    base_contrast = 10.0
    plan = EditPlan(genre="sports", global_=GlobalAdjust(contrast=base_contrast, exposure=0.2))
    learner.apply(plan, "sports", default_profile_for("sports"))
    shift = plan.global_.contrast - base_contrast
    # the model stores a normalised preference (|value| <= MAX_INFLUENCE) and the
    # apply step scales it into Lightroom units
    assert abs(shift) <= MAX_INFLUENCE["contrast"] * 30.0 + 1e-6, shift
    assert abs(shift) > 0.5, "strong feedback should move the plan"


def test_rejections_pull_the_model_back_toward_neutral(learner, settings):
    settings.personalised_learning = True
    for _ in range(4):
        learner.record("manual_adjust", "sports", manual_delta={"contrast": 20})
    raised = learner.model["sports"].contrast
    assert raised > 0
    for _ in range(4):
        learner.record("reject", "sports")
    assert learner.model["sports"].contrast < raised, "rejection reduces the learned bias"


def test_needs_at_least_three_samples(learner, settings):
    settings.personalised_learning = True
    learner.record("manual_adjust", "sports", manual_delta={"contrast": 10})
    plan = EditPlan(genre="sports", global_=GlobalAdjust(contrast=10.0))
    result = learner.apply(plan, "sports", default_profile_for("sports"))
    assert not result.get("applied")
    assert plan.global_.contrast == 10.0


def test_reset_removes_the_model(learner, settings):
    settings.personalised_learning = True
    for _ in range(5):
        learner.record("accept", "sports", rating=5)
    learner.reset()
    assert learner.model == {}
    assert json.loads(learner.export_model())["model"] == {}


def test_model_is_transportable(learner, settings):
    settings.personalised_learning = True
    for _ in range(5):
        learner.record("manual_adjust", "sports", manual_delta={"vibrance": -6})
    document = json.loads(learner.export_model())
    assert document["format"] == "kestrel-personalisation"
    assert "sports" in document["model"]
    entry = document["model"]["sports"]
    for key in ("genre", "samples", "contrast", "vibrance", "last_updated"):
        assert key in entry, key
