"""Colour science: transfer functions, Lab accuracy, white balance, gamut."""
from __future__ import annotations

import numpy as np
import pytest

from kestrel import color


def test_srgb_transfer_round_trip_is_exact_enough():
    x = np.linspace(0, 1, 20001, dtype=np.float32)
    assert float(np.abs(color.linear_to_srgb(color.srgb_to_linear(x)) - x).max()) < 1e-5


def test_lab_round_trip():
    rng = np.random.default_rng(3)
    rgb = rng.random((64, 64, 3)).astype(np.float32)
    lab = color.rgb_to_lab(rgb)
    back = color.lab_to_rgb(lab)
    assert float(np.abs(back - rgb).max()) < 1e-4


def test_lab_reference_white_and_black():
    white = np.ones((1, 1, 3), np.float32)
    lab = color.rgb_to_lab(white)
    assert abs(float(lab[0, 0, 0]) - 100.0) < 0.2
    assert abs(float(lab[0, 0, 1])) < 0.2 and abs(float(lab[0, 0, 2])) < 0.2
    black_lab = color.rgb_to_lab(np.zeros((1, 1, 3), np.float32))
    assert abs(float(black_lab[0, 0, 0])) < 0.2


def test_lab_types_stay_float32():
    lab = color.rgb_to_lab(np.random.random((8, 8, 3)).astype(np.float32))
    assert lab.dtype == np.float32


def test_delta_e_bounds():
    a = color.rgb_to_lab(np.full((2, 2, 3), 0.5, np.float32))
    assert float(color.delta_e76(a, a).max()) == 0.0
    b = color.rgb_to_lab(np.full((2, 2, 3), 0.6, np.float32))
    assert float(color.delta_e76(a, b).min()) > 1.0


def test_cct_of_d65_is_daylight():
    cct, duv = color.xy_to_cct_tint(*color.D65_XY)
    assert 6200 <= cct <= 6700, cct
    assert abs(duv) < 0.01


def test_cct_monotonic_with_xy_temperature():
    warm = color.xy_to_cct_tint(0.48, 0.41)[0]
    cool = color.xy_to_cct_tint(0.26, 0.28)[0]
    assert warm < cool


def test_white_balance_corrects_toward_neutral():
    """The slider names the *source* illuminant; correcting 4200K adds blue."""
    grey = np.full((8, 8, 3), 0.18, np.float32)
    warm_source = color.apply_wb(grey, 4200, 0)
    cool_source = color.apply_wb(grey, 9000, 0)
    neutral = color.apply_wb(grey, 6500, 0)
    assert warm_source[..., 2].mean() > neutral[..., 2].mean() > cool_source[..., 2].mean()
    assert warm_source[..., 0].mean() < neutral[..., 0].mean() < cool_source[..., 0].mean()


def test_tint_moves_along_green_magenta_axis():
    grey = np.full((8, 8, 3), 0.18, np.float32)
    magenta = color.apply_wb(grey, 6500, 25)
    green = color.apply_wb(grey, 6500, -25)
    assert magenta[..., 1].mean() < green[..., 1].mean(), "positive tint suppresses green"


def test_kelvin_xy_matches_known_points():
    x, y = color.kelvin_to_xy(6500)
    assert 0.28 < x < 0.35 and 0.30 < y < 0.36


def test_estimate_illuminant_recovers_a_known_cast():
    grey = np.full((64, 64, 3), 0.5, np.float32)
    grey[..., 2] *= 0.82          # blue-deficient light
    scene, confidence = color.estimate_illuminant(grey)
    assert scene[2] < scene[0], scene      # light is blue-deficient
    assert 1.0 / scene[2] > 1.15, "correction lifts blue"
    assert 0.0 <= confidence <= 1.0


def test_gamut_compress_keeps_range():
    from kestrel.render.core import gamut_compress

    rng = np.random.default_rng(1)
    img = rng.random((32, 32, 3)).astype(np.float32) * 1.4 - 0.2
    out = gamut_compress(img)
    assert out.min() >= 0 and out.max() <= 1
    assert np.isfinite(out).all()
