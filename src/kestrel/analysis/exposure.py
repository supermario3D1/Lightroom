"""Exposure, dynamic-range and tone analysis."""
from __future__ import annotations

from typing import Optional

import numpy as np

from .. import color
from ..core import clamp
from ..models import ExposureInfo, SceneInfo

TARGET_LUMA = {
    # median display luma targets per genre — conservative, publication-like
    "sports": 0.44, "wedding": 0.52, "portrait": 0.48, "event": 0.45,
    "landscape": 0.45, "wildlife": 0.45, "commercial": 0.55, "general": 0.46,
}
FACE_TARGET = 0.615     # editorial skin-luminance target (L* ≈ 68–72)
SUBJECT_TARGET = 0.50


def histogram_luma(gray: np.ndarray, bins: int = 64) -> list:
    hist, _ = np.histogram(gray, bins=bins, range=(0.0, 1.0))
    hist = hist.astype(np.float64)
    hist /= max(hist.sum(), 1.0)
    return [round(float(v), 6) for v in hist]


def _median_of(gray: np.ndarray, mask: Optional[np.ndarray], fallback: float) -> float:
    if mask is None:
        return fallback
    sel = mask > 0.5
    if sel.sum() < 200:
        return fallback
    return float(np.median(gray[sel]))


def analyse_exposure(rgb: np.ndarray, subject: Optional[np.ndarray],
                     face: Optional[np.ndarray], background: Optional[np.ndarray],
                     scene: Optional[SceneInfo] = None) -> ExposureInfo:
    gray = color.luma_display(rgb)
    lin = color.srgb_to_linear(gray)
    pcts = [float(np.percentile(gray, q)) for q in (1, 5, 25, 50, 75, 95, 99, 99.9)]
    mean_l = float(gray.mean())
    median_l = pcts[3]
    subject_l = _median_of(gray, subject, mean_l)
    face_l = _median_of(gray, face, subject_l)
    bg_l = _median_of(gray, background, mean_l)
    clip_hi = float((gray > 0.995).mean())
    clip_lo = float((gray < 0.005).mean())

    # Recoverable headroom estimated from the fraction of near-clipped pixels and
    # how much tone remains between p99.9 and white.
    head_hi = clamp((1.0 - pcts[7]) * 14.0, 0.0, 1.6)
    if clip_hi > 0.01:
        head_hi = min(head_hi, 0.55)
    elif clip_hi > 0.002:
        head_hi = min(head_hi, 0.9)
    shadow_head = clamp(np.log2((float(np.percentile(lin, 99.5)) + 1e-4) /
                                (float(np.percentile(lin, 0.5)) + 1e-6)), 3.0, 15.0)

    genre = (scene.genre_scores and max(scene.genre_scores, key=scene.genre_scores.get)) if scene else "general"
    target = TARGET_LUMA.get(genre or "general", 0.46)
    if scene is not None:
        if scene.night > 0.55:
            target -= 0.06          # night frames should stay night
        if scene.overcast > 0.5:
            target += 0.01
    # Weighted: faces matter most, then the subject, then the frame.
    hard = 0.55 * face_l + 0.30 * subject_l + 0.15 * median_l
    if face is None or float((face > 0.5).sum()) < 400:
        hard = 0.65 * subject_l + 0.35 * median_l
    lt = max(color.srgb_to_linear(np.float32(np.clip(target, 0.05, 0.95))), 1e-4)
    lc = max(color.srgb_to_linear(np.float32(np.clip(hard, 0.02, 0.98))), 1e-4)
    ev = float(clamp(np.log2(float(lt) / float(lc)) * 0.82, -2.2, 2.2))

    info = ExposureInfo(
        mean_luma=mean_l, median_luma=median_l,
        p01=pcts[0], p05=pcts[1], p25=pcts[2], p75=pcts[4], p95=pcts[5], p99=pcts[6],
        clipping_high=clip_hi, clipping_low=clip_lo,
        headroom_high=head_hi, headroom_low=shadow_head,
        subject_luma=subject_l, face_luma=face_l, background_luma=bg_l,
        ev_bias=ev, dynamic_range_stops=float(shadow_head),
        histogram=histogram_luma(gray),
    )
    return info


def calibrate_target_from_reference(rgb: np.ndarray, skin_mask: np.ndarray) -> float:
    """User-facing helper: what luminance does *this* photographer's skin edits sit at?

    Used by personalisation to learn per-photographer target brightness.
    """
    if skin_mask is None or (skin_mask > 0.5).sum() < 300:
        return FACE_TARGET
    gray = color.luma_display(rgb)
    return float(np.median(gray[skin_mask > 0.5]))
