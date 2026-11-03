"""The Lightroom gateway is where honesty has to be structural.

These tests pin the delivery contract the plug-in relies on:

  * develop settings are the documented 2012 keys only — Lightroom rejects a
    settings table containing anything else, and a rejected table aborts a
    whole batch;
  * *every* mask is reported with the ``render`` capability, because the
    documented SDK has no call that creates a local adjustment mask. Claiming
    ``native`` there would be exactly the kind of invented capability this
    project refuses to ship;
  * the three delivery modes really differ — ``native`` carries no mask plan at
    all, ``adaptive`` carries it for review and re-rendering.
"""
from __future__ import annotations

from kestrel.lightroom import native
from kestrel.models import EditPlan, GlobalAdjust, MaskLayer


def make_plan() -> EditPlan:
    plan = EditPlan(genre="sports", intent="automatic")
    plan.global_.exposure = 0.35
    plan.global_.highlights = -32.0
    plan.global_.shadows = 28.0
    plan.global_.saturation = 6.0
    plan.masks = [
        MaskLayer(name="Subject", kind="subject", confidence=0.62),
        MaskLayer(name="Background", kind="background", opacity=0.8),
        MaskLayer(name="Sky gradient", kind="linear", geometry={"x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 0.4}),
        MaskLayer(name="Sun", kind="radial", geometry={"cx": 0.7, "cy": 0.2, "rx": 0.2, "ry": 0.2}),
        MaskLayer(name="Disabled layer", kind="sky", enabled=False),
    ]
    return plan


def test_mask_capability_is_render_for_everything():
    payload = native.local_mask_payload(make_plan())
    assert payload["format"] == "kestrel-masks"
    assert payload["version"] == 1
    kinds = {mask["kind"]: mask for mask in payload["masks"]}
    assert set(kinds) == {"subject", "background", "linear", "radial"}, "disabled layers are dropped"
    for mask in payload["masks"]:
        assert mask["capability"] == "render", mask["kind"]


def test_masks_record_how_they_could_be_expressed():
    payload = native.local_mask_payload(make_plan())
    expressible = {mask["kind"]: mask["expressible"] for mask in payload["masks"]}
    assert expressible["linear"] == "gradient"
    assert expressible["radial"] == "gradient"
    assert expressible["subject"] == "ai"


def test_capability_report_never_claims_native_masks():
    report = native.capability_report()
    assert report["global_settings"] == "native"
    for key in ("local_masks", "radial_linear_masks", "ai_masks"):
        assert key in report, key
        assert report[key].startswith("render mode"), f"{key} must not claim native support"
    assert report["object_removal"] == "not implemented"
    assert report["perspective_correction"].startswith("native")


def test_native_mode_carries_no_mask_plan():
    plan = make_plan()
    adaptive = native.apply_payload(plan, None, "img_test", "ver_test", "adaptive")
    plain = native.apply_payload(plan, None, "img_test", "ver_test", "native")
    assert len(adaptive["masks"]["masks"]) == 4
    assert plain["masks"]["masks"] == []
    assert plain["masks"]["format"] == "kestrel-masks"
    assert plain["mode"] == "native"


def test_payload_is_versioned_and_names_no_unknown_settings():
    allowed = set(native.DEVELOP_KEYS) if hasattr(native, "DEVELOP_KEYS") else None
    payload = native.apply_payload(make_plan(), None, "img_test", "ver_test", "adaptive")
    settings = payload["settings"]
    assert settings, "settings must not be empty"
    assert all(isinstance(key, str) and key for key in settings)
    assert "Exposure2012" in settings and "Highlights2012" in settings
    if allowed is not None:
        assert set(settings) <= allowed, sorted(set(settings) - allowed)
    assert payload["schema"] == native.BRAND["engine_schema"]
    assert payload["image_id"] == "img_test"
    assert payload["version_id"] == "ver_test"
    assert isinstance(payload["explanation"], list)
    assert isinstance(payload["warnings"], list)


def test_to_develop_settings_clamps_into_lightroom_ranges():
    plan = EditPlan()
    plan.global_ = GlobalAdjust(exposure=9.0, contrast=500.0, saturation=-400.0, clarity=250.0)
    settings = native.to_develop_settings(plan, None)
    assert -5.0 <= settings.get("Exposure2012", 0.0) <= 5.0
    assert -100.0 <= settings.get("Contrast2012", 0.0) <= 100.0
    assert -100.0 <= settings.get("Saturation", 0.0) <= 100.0
