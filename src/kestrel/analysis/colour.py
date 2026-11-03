"""Colour, white balance and tonal-palette analysis."""
from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

from .. import color
from ..core import clamp
from ..models import ColourInfo, QualityInfo
from .cluster import kmeans_simple


def analyse_colour(rgb: np.ndarray, skin: Optional[np.ndarray], grass: Optional[np.ndarray],
                   sky: Optional[np.ndarray], quality: QualityInfo) -> ColourInfo:
    h, w = rgb.shape[:2]
    hsv = cv2.cvtColor(np.clip(rgb, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
    sat = hsv[..., 1]
    val = hsv[..., 2]
    visible = val > 0.06
    sat_vals = sat[visible] if visible.any() else sat.reshape(-1)

    exclude = np.zeros((h, w), np.float32)
    for m in (skin, grass, sky):
        if m is not None:
            exclude = np.maximum(exclude, m)
    samples = color.neutral_samples(rgb, exclude=exclude if exclude.any() else None)
    temp, tint, cast, wb_conf = color.solve_wb(samples_linear=samples)

    # A cast is only "real" when the neutral reference agrees with itself.
    cast_strength = float(cast)
    if wb_conf < 0.25:
        cast_strength *= 0.4

    # Palette from a coarse downsample; used for the UI and consistency checks.
    small = cv2.resize(rgb, (160, max(1, int(160 * h / max(w, 1)))), interpolation=cv2.INTER_AREA)
    px = small.reshape(-1, 3)
    palette = kmeans_simple(px, k=5, max_points=6000)
    dominant = [[round(float(c), 4) for c in centre] for centre, share in palette if share > 0.03][:5]

    info = ColourInfo(
        temperature=float(temp), tint=float(tint), cast_strength=cast_strength,
        cast_confidence=float(wb_conf), wb_confidence=float(wb_conf),
        saturation_mean=float(np.mean(sat_vals)) if sat_vals.size else 0.0,
        saturation_p95=float(np.percentile(sat_vals, 95)) if sat_vals.size else 0.0,
        vibrance_need=float(clamp((0.30 - float(np.percentile(sat_vals, 75))) / 0.25, 0, 1)) if sat_vals.size else 0.0,
        chroma_noise=float(quality.noise_chroma),
        dominant_colours=dominant,
        grey_balance=float(quality.noise_sigma * 0),
    )

    if skin is not None and (skin > 0.55).sum() > 400:
        skin_px = rgb[skin > 0.55]
        lab = color.rgb_to_lab(skin_px).reshape(-1, 3)
        mean_lab = lab.mean(axis=0)
        info.skin_lab = [float(v) for v in mean_lab]
        hsv_skin = cv2.cvtColor((np.clip(skin_px, 0, 1)[None, :, :]).astype(np.float32), cv2.COLOR_RGB2HSV)[0]
        info.skin_hue = float(np.median(hsv_skin[:, 0]))  # already 0..360 for float input
        info.skin_saturation = float(np.median(hsv_skin[:, 1]))
    info.is_monochrome = bool(info.saturation_p95 < 0.045 and info.saturation_mean < 0.03)
    return info
