"""Renderer: physics, locality, determinism, high-bit-depth behaviour."""
from __future__ import annotations

import copy

import numpy as np
import pytest

from kestrel import color
from kestrel.models import EditPlan, GlobalAdjust, MaskLayer
from kestrel.render.core import RenderConfig, Renderer, gamut_compress


@pytest.fixture(scope="module")
def renderer():
    return Renderer(RenderConfig(max_edge=800, quality="high", noise_refine=True))


def _plan(**kw):
    plan = EditPlan(genre="general", intent="natural", quality="high", iterations=1)
    for key, value in kw.items():
        setattr(plan.global_, key, value)
    return plan


def test_render_is_deterministic(renderer, analysis):
    _, masks, decoded = analysis
    mask_dict = {"subject": masks.subject, "background": masks.background}
    plan = _plan(exposure=0.3, contrast=12, vibrance=10)
    a, _ = renderer.render(decoded.rgb, plan, mask_dict)
    b, _ = renderer.render(decoded.rgb, plan, mask_dict)
    assert np.array_equal(a, b), "same input and plan must give identical pixels"


def test_output_is_finite_and_in_range(renderer, analysis):
    _, masks, decoded = analysis
    out, report = renderer.render(decoded.rgb, _plan(exposure=0.5, contrast=20, saturation=15),
                                  {"subject": masks.subject})
    assert out.dtype == np.float32
    assert max(out.shape[:2]) == 800            # the configured preview size
    ratio_in = decoded.rgb.shape[0] / decoded.rgb.shape[1]
    ratio_out = out.shape[0] / out.shape[1]
    assert abs(ratio_in - ratio_out) < 0.01
    assert np.isfinite(out).all()
    assert out.min() >= 0.0 and out.max() <= 1.0
    assert report.ms_total > 0
    assert "tone" in report.ms_stages


def test_exposure_moves_the_image(renderer):
    from conftest import flat_patch

    dark = flat_patch(240, 160, value=(0.25, 0.25, 0.25))
    brighter, _ = renderer.render(dark, _plan(exposure=1.0))
    darker, _ = renderer.render(dark, _plan(exposure=-1.0))
    assert brighter.mean() > dark.mean() > darker.mean()


def test_highlight_recovery_pulls_back_clipping(renderer):
    from conftest import night_sports

    img = np.clip(night_sports() * 1.9, 0, 1)
    recovered, _ = renderer.render(img, _plan(highlights=-80, whites=-10))
    plain, _ = renderer.render(img, _plan(highlights=0, whites=0))
    clip_before = float((plain > 0.995).mean())
    clip_after = float((recovered > 0.995).mean())
    assert clip_after <= clip_before + 1e-3


def test_shadow_lift_brightens_dark_regions(renderer):
    from conftest import night_sports

    img = night_sports() * 0.5
    lifted, _ = renderer.render(img, _plan(shadows=70, blacks=10))
    plain, _ = renderer.render(img, _plan(shadows=0, blacks=0))
    dark = img.mean(axis=2) < 0.08
    assert lifted[dark].mean() > plain[dark].mean()


def test_masked_adjustment_is_local(renderer):
    from conftest import night_sports

    img = night_sports()
    h, w = img.shape[:2]
    mask = np.zeros((h, w), np.float32)
    mask[:, : w // 3] = 1.0
    plan = _plan()
    plan.masks = [MaskLayer(name="Left", kind="subject", opacity=1.0, feather=0.0,
                            adjustments=GlobalAdjust(exposure=1.0, saturation=20))]
    out, _ = renderer.render(img, plan, {"subject": mask})
    left = out[:, : w // 3 - 4].mean()
    right = out[:, 2 * w // 3:].mean()
    assert left > img[:, : w // 3 - 4].mean() * 1.2
    assert abs(right - img[:, 2 * w // 3:].mean()) < 0.02, "mask must not leak"


def test_negative_dehaze_does_not_invert_sky_colour(renderer):
    from conftest import daylight_landscape

    img = daylight_landscape()
    h, w = img.shape[:2]
    sky = img[: h // 2]
    out, _ = renderer.render(img, _plan(dehaze=-40))
    out_sky = out[: h // 2]
    lab_before = color.rgb_to_lab(sky).reshape(-1, 3).mean(axis=0)
    lab_after = color.rgb_to_lab(out_sky).reshape(-1, 3).mean(axis=0)
    # negative dehaze adds atmosphere: slightly lower contrast, but the sky must
    # stay blue (negative b*) rather than turning magenta (positive b*)
    assert lab_before[2] < 0 and lab_after[2] < 0, (lab_before, lab_after)
    assert float(np.abs(out_sky - sky).max()) < 0.6


def test_sharpening_is_edge_limited(renderer):
    """A hard edge must gain detail without a large overshoot halo."""
    h, w = 240, 320
    img = np.full((h, w, 3), 0.35, np.float32)
    img[:, w // 2:] = 0.65
    out, _ = renderer.render(img, _plan(sharpness=110, sharpen_masking=40), {})
    row = out[h // 2, :, 0]
    over = float(max(0.0, row[w // 2 - 3: w // 2 + 3].max() - 0.65))
    under = float(max(0.0, 0.35 - row[w // 2 - 3: w // 2 + 3].min()))
    assert over < 0.12 and under < 0.12, (over, under)
    assert row[w // 2 - 1] < row[w // 2 + 1]


def test_vignette_darkens_corners(renderer):
    from conftest import flat_patch

    img = flat_patch(320, 240, value=(0.55, 0.55, 0.55))
    out, _ = renderer.render(img, _plan(post_crop_vignette=-60))
    corner = out[:20, :20].mean()
    centre = out[100:140, 140:180].mean()
    assert corner < centre - 0.02


def test_gamut_compression_keeps_hue_family():
    lab = np.zeros((6, 6, 3), np.float32)
    lab[..., 0] = 65
    lab[..., 1] = 90
    lab[..., 2] = 80
    rgb = color.lab_to_rgb(lab)
    squeezed = gamut_compress(rgb)
    assert squeezed.min() >= 0 and squeezed.max() <= 1
    hue_before = np.arctan2(lab[..., 2].mean(), lab[..., 1].mean())
    lab_after = color.rgb_to_lab(squeezed)
    hue_after = np.arctan2(lab_after[..., 2].mean(), lab_after[..., 1].mean())
    assert abs(hue_after - hue_before) < np.deg2rad(18)


def test_16bit_precision_survives_rendering(renderer):
    """A smooth ramp must keep far more than 8-bit steps after tone work."""
    ramp = np.linspace(0.02, 0.98, 512, dtype=np.float32)[None, :, None].repeat(64, 0)
    img = np.repeat(ramp, 3, axis=2)
    out, _ = renderer.render(img, _plan(contrast=10, shadows=10))
    row = out[32, :, 0]
    assert len(np.unique(np.round(row * 65535).astype(np.int32))) > 200


def test_plan_is_not_mutated_by_rendering(renderer, analysis):
    _, masks, decoded = analysis
    plan = _plan(exposure=0.4, contrast=8)
    snapshot = plan.to_json()
    renderer.render(decoded.rgb, plan, {"subject": masks.subject})
    assert plan.to_json() == snapshot
