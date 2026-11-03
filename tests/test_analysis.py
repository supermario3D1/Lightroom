"""Analysis: scene understanding, exposure, colour, quality, genre."""
from __future__ import annotations

import numpy as np
import pytest

from kestrel import imageio
from kestrel.analysis.analyser import Analyser


@pytest.fixture(scope="module")
def analyser():
    return Analyser(model_dir=None)


def _analyse(analyser, rgb, edge=800):
    decoded = imageio.DecodedImage(rgb=rgb, meta=imageio.ImageMetadata(
        width=rgb.shape[1], height=rgb.shape[0], bit_depth=8), scale=1.0, source="synthetic")
    result, masks = analyser.analyse(decoded, image_id="synthetic", analysis_edge=edge)
    return result, masks


def test_sports_scene_is_recognised(analysis):
    result, masks, _ = analysis
    assert result.genre in ("sports", "event"), result.genre
    assert result.genre_confidence > 0.4
    scene = result.scene
    assert scene.stadium > 0.2 or scene.grass_coverage > 0.1
    assert scene.outdoor >= scene.indoor
    assert result.subject_coverage > 0.01
    assert masks.subject is not None and masks.background is not None


def test_exposure_describes_the_capture(analysis):
    result, _, decoded = analysis
    exp = result.exposure
    assert exp.p01 <= exp.p05 <= exp.p95 <= exp.p99
    assert 0.0 <= exp.mean_luma <= 1.0
    assert 0.0 <= exp.clipping_high <= 0.2
    assert exp.p05 <= exp.p95
    assert 0.0 <= exp.subject_luma <= 1.0


def test_colour_has_a_plausible_white_balance(analysis):
    result, _, _ = analysis
    colour = result.colour
    assert 2500 <= colour.temperature <= 12000
    assert -60 <= colour.tint <= 60
    assert 0.0 <= colour.saturation_mean <= 1.0
    assert colour.saturation_p95 >= colour.saturation_mean
    assert 0.0 <= colour.cast_strength <= 1.0


def test_quality_metrics_are_calibrated(analysis):
    result, _, _ = analysis
    q = result.quality
    # noise_sigma is stored in 0..100 units (display sigma × 100) so it lines up
    # with the other quality numbers the UI shows.
    assert 0.0 <= q.noise_sigma <= 20.0, q.noise_sigma
    assert q.iso_estimate >= 50
    assert 0.0 <= q.sharpness <= 1.0
    assert 0.0 <= q.motion_blur <= 1.0
    assert 0.0 <= q.compression_artifacts <= 1.0


def test_masks_are_soft_mattes(analysis):
    _, masks, decoded = analysis
    h, w = decoded.rgb.shape[:2]
    for name in ("subject", "background", "face", "skin", "grass", "crowd"):
        mask = masks.get(name)
        if mask is None:
            continue
        assert mask.shape == (h, w), name
        assert mask.dtype == np.float32
        assert 0.0 <= float(mask.min()) and float(mask.max()) <= 1.0
    if masks.subject is not None and masks.background is not None:
        overlap = float((masks.subject * masks.background).mean())
        assert overlap < 0.2, "subject and background must be complementary, not identical"


def test_dark_image_is_read_as_underexposed(analyser):
    from conftest import night_sports

    rgb = night_sports() * 0.45
    result, _ = _analyse(analyser, rgb.astype(np.float32))
    assert result.exposure.mean_luma < 0.35
    assert result.scene.night > 0.25 or result.scene.artificial > 0.3


def test_bright_image_is_read_as_bright(analyser):
    from conftest import daylight_landscape

    rgb = np.clip(daylight_landscape() * 1.35, 0, 1)
    result, _ = _analyse(analyser, rgb.astype(np.float32))
    assert result.exposure.p95 > 0.7
    assert result.scene.outdoor > 0.4


def test_flat_grey_is_low_contrast(analyser):
    from conftest import flat_patch

    result, _ = _analyse(analyser, flat_patch(320, 240))
    spread = result.exposure.p95 - result.exposure.p05
    assert spread < 0.06
    assert result.colour.saturation_mean < 0.12


def test_noise_estimate_responds_to_noise(analyser):
    from conftest import flat_patch

    clean = flat_patch(320, 240, value=(0.35, 0.35, 0.35))
    rng = np.random.default_rng(2)
    noisy = np.clip(clean + rng.normal(0, 0.05, clean.shape).astype(np.float32), 0, 1)
    clean_q = _analyse(analyser, clean)[0].quality
    noisy_q = _analyse(analyser, noisy)[0].quality
    assert noisy_q.noise_sigma > clean_q.noise_sigma * 1.5
    assert noisy_q.iso_estimate >= clean_q.iso_estimate


def test_analysis_is_deterministic(analyser):
    from conftest import night_sports

    rgb = night_sports().astype(np.float32)
    a = _analyse(analyser, rgb)[0].to_dict()
    b = _analyse(analyser, rgb)[0].to_dict()
    for key in ("genre", "genre_confidence", "exposure", "colour", "quality", "scene"):
        assert a[key] == b[key], key


def test_providers_are_declared(analysis):
    result, _, _ = analysis
    assert "faces" in result.providers
    assert "segmentation" in result.providers
    assert result.providers["faces"] != ""
