"""Objective image-quality metrics used to critique and refine edits.

These metrics are deliberately *not* "more is better" proxies.  Each has an
acceptable range and both under- and over-processing are penalised.
"""
from __future__ import annotations

from typing import Dict, Optional

import cv2
import numpy as np

from .. import color
from ..analysis.quality import estimate_noise, estimate_sharpness
from ..core import clamp
from ..models import EditMetrics, EditPlan, ExplanationItem

# Publication-grade targets per genre: (contrast_p05_p95, saturation_mean, clip_high)
TARGETS: Dict[str, Dict[str, float]] = {
    "sports":     {"contrast": 0.56, "saturation": 0.34, "clip": 0.004, "face": 0.615},
    "wedding":    {"contrast": 0.46, "saturation": 0.26, "clip": 0.002, "face": 0.640},
    "portrait":   {"contrast": 0.46, "saturation": 0.27, "clip": 0.002, "face": 0.620},
    "event":      {"contrast": 0.50, "saturation": 0.31, "clip": 0.004, "face": 0.605},
    "landscape":  {"contrast": 0.55, "saturation": 0.32, "clip": 0.003, "face": 0.600},
    "wildlife":   {"contrast": 0.54, "saturation": 0.30, "clip": 0.004, "face": 0.600},
    "commercial": {"contrast": 0.50, "saturation": 0.27, "clip": 0.002, "face": 0.620},
    "general":    {"contrast": 0.49, "saturation": 0.28, "clip": 0.003, "face": 0.610},
}

INTENT_TARGET_SCALE = {
    "natural": 0.88, "vibrant": 1.12, "dramatic": 1.18, "editorial": 1.0,
    "automatic": 1.0, "custom": 1.0,
}


def _mask_mean(img: np.ndarray, mask: Optional[np.ndarray], fallback: float) -> float:
    if mask is None:
        return fallback
    sel = mask > 0.5
    if sel.sum() < 50:
        return fallback
    return float(img[sel].mean())


def _bandpass(img: np.ndarray, small: float = 1.6, large: float = 7.0) -> np.ndarray:
    return (cv2.GaussianBlur(img, (0, 0), small) - cv2.GaussianBlur(img, (0, 0), large))


def _halo(orig_l: np.ndarray, edit_l: np.ndarray) -> float:
    """Local overshoot introduced near edges — isolated from global tone changes.

    A global shadow lift legitimately changes edge-band luminance, so we compare the
    *band-passed* images: halos are extra local energy around strong edges.
    """
    bp_o = _bandpass(orig_l)
    bp_e = _bandpass(edit_l)
    gx = cv2.Sobel(orig_l, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(orig_l, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    edges = mag > max(float(np.percentile(mag, 97)), 0.05)
    if edges.sum() < 150:
        return 0.0
    k = np.ones((7, 7), np.uint8)
    band = cv2.dilate(edges.astype(np.uint8), k).astype(bool) & ~edges
    if band.sum() < 150:
        band = edges
    excess = np.abs(bp_e[band]) - np.abs(bp_o[band]) - 0.010
    if excess.size == 0:
        return 0.0
    return float(clamp(float(np.percentile(excess, 98)) * 9.0, 0.0, 1.0))


def _skin_delta(orig: np.ndarray, edit: np.ndarray, skin: Optional[np.ndarray]) -> float:
    """Chromatic drift of skin between capture and edit.

    Uses the median, not the mean: a few specular pixels on a forehead (or a
    white collar caught by the mask) would otherwise dominate the number.  Blown
    skin is excluded because there is no colour left in it to judge, and a mask
    covering less than 0.1 % of the frame is treated as "not measurable" rather
    than reported as a colour error.
    """
    if skin is None:
        return 0.0
    m = skin > 0.6
    if m.sum() < max(300, int(0.001 * m.size)):
        return 0.0
    lab_o = color.rgb_to_lab(orig)
    lab_e = color.rgb_to_lab(edit)
    usable = m & (lab_o[..., 0] < 88.0) & (lab_o[..., 0] > 12.0)
    sel = usable if usable.sum() >= 200 else m
    d = np.median(lab_e[sel], axis=0) - np.median(lab_o[sel], axis=0)
    # allow natural brightening; penalise hue/saturation drift much more
    return float(np.sqrt((d[0] * 0.35) ** 2 + d[1] ** 2 + d[2] ** 2))


def _skin_consistency(edit: np.ndarray, skin: Optional[np.ndarray]) -> float:
    if skin is None or (skin > 0.6).sum() < 300:
        return 1.0
    if float((skin > 0.6).mean()) < 0.0015:
        return 1.0
    lab = color.rgb_to_lab(edit)
    m = skin > 0.6
    h, w = m.shape
    # split skin into a coarse grid and compare median skin colour per cell
    cells = []
    for by in range(4):
        for bx in range(4):
            sub = m[by * h // 4:(by + 1) * h // 4, bx * w // 4:(bx + 1) * w // 4]
            if sub.sum() > 120:
                ys, xs = np.nonzero(sub)
                vals = lab[by * h // 4:(by + 1) * h // 4, bx * w // 4:(bx + 1) * w // 4][ys, xs]
                cells.append(np.median(vals, axis=0))
    if len(cells) < 2:
        return 1.0
    arr = np.array(cells)
    med = np.median(arr, axis=0)
    dev = np.abs(arr - med).sum(axis=1)
    # Robust: ignore the worst 25% of cells so a stray face in coloured light
    # does not dominate a whole-frame consistency score.
    dev = np.sort(dev)[: max(1, int(len(dev) * 0.75))]
    spread = float(np.median(dev))
    return float(clamp(1.0 - spread / 16.0, 0.0, 1.0))


def _accept_range(value: float, lo: float, hi: float) -> float:
    """Penalty 0..1 for being outside an acceptable interval."""
    if value < lo:
        return (lo - value) / max(lo, 1e-3)
    if value > hi:
        return (value - hi) / max(hi, 1e-3)
    return 0.0


def _fit_mask(mask: Optional[np.ndarray], shape: tuple) -> Optional[np.ndarray]:
    """Masks may be built at analysis resolution and used at render resolution."""
    if mask is None:
        return None
    if mask.shape[:2] == shape[:2]:
        return mask
    return cv2.resize(mask.astype(np.float32), (shape[1], shape[0]),
                      interpolation=cv2.INTER_LINEAR)


def compute_metrics(original: np.ndarray, edited: np.ndarray, masks: Dict[str, np.ndarray],
                    genre: str, intent: str = "automatic",
                    face_mask: Optional[np.ndarray] = None) -> EditMetrics:
    shape = edited.shape[:2]
    masks = {k: _fit_mask(v, shape) for k, v in (masks or {}).items() if v is not None}
    if face_mask is not None:
        face_mask = _fit_mask(face_mask, shape)
    if original.shape[:2] != shape:
        original = cv2.resize(original, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)
    t = TARGETS.get(genre, TARGETS["general"])
    scale = INTENT_TARGET_SCALE.get(intent, 1.0)
    # Expected outcome is judged relative to what the capture already carried:
    # an already-punchy frame is not rewarded for getting punchier.
    lo0 = color.luma_display(original)
    in_contrast = float(np.percentile(lo0, 95) - np.percentile(lo0, 5))
    in_hsv = cv2.cvtColor(np.clip(original, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
    in_sat = float(np.mean(in_hsv[..., 1][in_hsv[..., 2] > 0.06])) if np.any(in_hsv[..., 2] > 0.06) else 0.0
    # Acceptance windows are anchored to the capture, not to a fixed ideal: a
    # frame with deep night shadows is allowed (and expected) to stay wide-range.
    contrast_lo = max(0.30, in_contrast - 0.05)
    contrast_hi = max(contrast_lo + 0.08, min(in_contrast + 0.09 * scale,
                                              max(t["contrast"], in_contrast + 0.02)))
    # Saturation window: reducing an over-saturated capture toward a natural level
    # is correct, so the floor is capped rather than pinned to the input.
    sat_lo = max(0.13, min(in_sat * 0.93, t["saturation"] + 0.10))
    sat_hi = max(sat_lo + 0.05, min(in_sat * 1.14 * scale, max(t["saturation"], in_sat * 1.02)))
    sat_hi = min(sat_hi, 0.72)
    in_clip_lo = float((lo0 < 0.006).mean())
    lo = color.luma_display(original)
    le = color.luma_display(edited)
    p = np.percentile(le, [5, 95])
    contrast = float(p[1] - p[0])
    hsv = cv2.cvtColor(np.clip(edited, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
    sat = float(np.mean(hsv[..., 1][hsv[..., 2] > 0.06])) if np.any(hsv[..., 2] > 0.06) else 0.0
    skin = masks.get("skin")
    face = face_mask if face_mask is not None else masks.get("face")
    subject = masks.get("subject")
    background = masks.get("background")

    target_face = t["face"] + (0.015 if intent == "vibrant" else 0.0)
    # Night/arena frames are correctly darker than a daylight portrait: measure
    # them against a slightly lower skin target, the way a picture editor would.
    lo_orig = color.luma_display(original)
    shadow_frac = float((lo_orig < 0.06).mean())
    target_face -= 0.07 * clamp(shadow_frac / 0.25, 0.0, 1.0)
    band = 0.065
    face_l = _mask_mean(le, face, _mask_mean(le, subject, float(le.mean())))
    exposure_error = face_l - target_face
    subject_l = _mask_mean(le, subject, float(le.mean()))
    bg_l = _mask_mean(le, background, float(le.mean()))
    before_subject = _mask_mean(lo, subject, float(lo.mean()))
    before_bg = _mask_mean(lo, background, float(lo.mean()))

    noise = estimate_noise(edited)
    sharp = estimate_sharpness(edited, subject)
    haloing = _halo(lo, le)
    skin_de = _skin_delta(original, edited, skin if skin is not None else face)
    skin_cons = _skin_consistency(edited, skin if skin is not None else face)
    clip_hi = float((le > 0.995).mean())
    clip_lo = float((le < 0.006).mean())
    # An exposure inside a usable band is correct; only the excess outside counts.
    # (Skin is judged in a band around the target, not at one exact value.)
    exposure_excess = max(0.0, abs(face_l - target_face) - band)

    # Naturalness: 1 = believable photograph, 0 = over-processed
    nat = 1.0
    nat -= clamp(_accept_range(sat, sat_lo, sat_hi) * 0.9, 0, 0.35)
    nat -= clamp(skin_de / 22.0, 0, 0.35)
    nat -= clamp(exposure_excess / 0.12, 0, 0.3)
    nat -= clamp(haloing * 0.45, 0, 0.3)
    nat -= clamp((clip_hi - t["clip"]) * 22.0, 0, 0.3)
    # Shadow crushing is only a fault when the capture actually had shadow detail.
    if in_clip_lo < 0.01:
        nat -= clamp((clip_lo - in_clip_lo - 0.004) * 25.0, 0, 0.25)
    nat = float(clamp(nat, 0.0, 1.0))

    # Overall objective (used to choose between refinement passes).
    score = 1.0
    score -= clamp(exposure_excess / 0.10, 0, 0.55)
    score -= clamp((clip_hi - t["clip"]) / 0.01, 0, 0.8)
    score -= clamp(_accept_range(contrast, contrast_lo, contrast_hi) * 1.4, 0, 0.5)
    score -= clamp(_accept_range(sat, sat_lo, sat_hi) * 1.6, 0, 0.4)
    score -= clamp(skin_de / 20.0, 0, 0.6)
    score -= clamp(haloing * 0.35, 0, 0.35)
    score -= clamp((noise["sigma_luma"] - 0.012) / 0.03, 0, 0.3)
    score += clamp((sharp["sharpness"] - 0.35) * 0.15, -0.1, 0.1)
    score = float(clamp(score, 0.0, 1.0))

    return EditMetrics(
        exposure_error=float(exposure_error), clipping_high=clip_hi, clipping_low=clip_lo,
        contrast=contrast, saturation=sat, skin_delta_e=skin_de,
        skin_consistency=skin_cons, noise_level=noise["sigma_luma"] * 100.0,
        sharpness=sharp["sharpness"], haloing=haloing, naturalness=nat,
        subject_lift=float(subject_l - before_subject),
        background_lift=float(bg_l - before_bg), score=score,
    )


def refine_plan(plan: EditPlan, metrics: EditMetrics, genre: str, intent: str,
                masks: Dict[str, np.ndarray]) -> EditPlan:
    """One deterministic correction step driven by the critic's measurements.

    This is the "photographer looking at the result and adjusting" step: bounded,
    explainable and never a blind parameter search.
    """
    g = plan.global_
    t = TARGETS.get(genre, TARGETS["general"])
    scale = INTENT_TARGET_SCALE.get(intent, 1.0)
    reasons = []

    if metrics.clipping_high > t["clip"] * 1.6:
        excess = metrics.clipping_high - t["clip"]
        g.exposure = round(g.exposure - float(clamp(excess * 8.0, 0.05, 0.5)), 2)
        g.highlights = round(max(g.highlights - float(clamp(excess * 400.0, 8, 35)), -90), 1)
        reasons.append("protected clipped highlights")
    elif metrics.exposure_error < -0.075 and g.exposure < 2.0:
        g.exposure = round(g.exposure + 0.10, 2)
        reasons.append("lifted underexposed subject")
    elif metrics.exposure_error > 0.09 and g.exposure > -2.0:
        g.exposure = round(g.exposure - 0.08, 2)
        reasons.append("tamed overexposed subject")

    if abs(metrics.contrast - t["contrast"]) > 0.12:
        delta = 4 if metrics.contrast < t["contrast"] else -4
        g.contrast = float(clamp(g.contrast + delta, -60, 70))
        reasons.append("adjusted contrast toward the " + genre + " target")

    sat_target = t["saturation"] * scale
    if abs(metrics.saturation - sat_target) > 0.075:
        delta = 4 if metrics.saturation < sat_target else -4
        g.vibrance = float(clamp(g.vibrance + delta * 0.7, -40, 55))
        g.saturation = float(clamp(g.saturation + delta * 0.3, -35, 30))
        reasons.append("rebalanced colour intensity")

    if metrics.skin_delta_e > 9.0:
        g.saturation = float(clamp(g.saturation - 3, -35, 30))
        g.vibrance = float(clamp(g.vibrance - 3, -40, 55))
        for m in plan.masks:
            if m.kind in ("face", "skin"):
                m.adjustments.saturation = float(clamp(m.adjustments.saturation - 3, -30, 25))
        reasons.append("reduced skin colour shift")

    if metrics.haloing > 0.35:
        g.clarity = float(clamp(g.clarity * 0.72, -30, 60))
        g.texture = float(clamp(g.texture * 0.8, -30, 60))
        reasons.append("removed halo risk from local contrast")

    if metrics.noise_level > 3.2 and plan.quality != "standard":
        g.luminance_nr = float(clamp(g.luminance_nr + 8, 0, 100))
        plan.denoise = {**plan.denoise, "luma_boost": True}
        reasons.append("increased noise reduction for the measured noise floor")

    if reasons:
        plan.explanation.append(ExplanationItem(
            area="refinement",
            action="; ".join(reasons),
            reason="Automatic quality review of the rendered preview",
            amount="",
            confidence=0.9,
        ))
    plan.metrics = metrics
    return plan
