"""Genre-specific editing intelligence.

A recipe is the bridge between *understanding* a photograph (analysis) and
*rendering* it.  Everything here is photographic reasoning expressed as numbers:
how far a sports frame's subject should be lifted over its background, how much
white balance authority a wedding reception frame gets, how hard a landscape's
sky may be pulled down before it looks fake.

Recipes are deliberately conservative: the renderer renders, the metrics module
measures, and only then does a bounded refinement pass correct anything still
wrong.  Nothing is applied simply because a genre "usually" needs it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from .. import color
from ..core import clamp
from ..models import (AnalysisResult, EditPlan, ExplanationItem, GlobalAdjust,
                      MaskLayer, ProfileSpec)
from .intents import Intent, get_intent

GENRE_BASE: Dict[str, Dict[str, float]] = {
    "sports":     {"contrast": 24, "clarity": 14, "vibrance": 18, "saturation": 3,
                   "texture": 12, "shadows": 20, "highlights": -30, "whites": 7, "blacks": -7,
                   "subject_lift": 0.28, "bg_control": -0.10, "skin_face": 0.06,
                   "nr_bias": 0.35, "sharp": 62, "canvas": 6},
    "wedding":    {"contrast": 13, "clarity": 6, "vibrance": 10, "saturation": 0,
                   "texture": 7, "shadows": 26, "highlights": -24, "whites": 5, "blacks": -5,
                   "subject_lift": 0.14, "bg_control": -0.02, "skin_face": 0.10,
                   "nr_bias": 0.5, "sharp": 48, "canvas": 5},
    "portrait":   {"contrast": 15, "clarity": 7, "vibrance": 9, "saturation": 0,
                   "texture": 8, "shadows": 18, "highlights": -18, "whites": 4, "blacks": -5,
                   "subject_lift": 0.12, "bg_control": -0.06, "skin_face": 0.12,
                   "nr_bias": 0.45, "sharp": 52, "canvas": 5},
    "event":      {"contrast": 18, "clarity": 9, "vibrance": 14, "saturation": 1,
                   "texture": 9, "shadows": 24, "highlights": -22, "whites": 5, "blacks": -6,
                   "subject_lift": 0.18, "bg_control": -0.08, "skin_face": 0.08,
                   "nr_bias": 0.45, "sharp": 55, "canvas": 8},
    "landscape":  {"contrast": 18, "clarity": 13, "vibrance": 16, "saturation": 1,
                   "texture": 15, "shadows": 26, "highlights": -34, "whites": 8, "blacks": -7,
                   "subject_lift": 0.0, "bg_control": 0.0, "skin_face": 0.0,
                   "nr_bias": 0.3, "sharp": 60, "canvas": 0},
    "wildlife":   {"contrast": 17, "clarity": 10, "vibrance": 12, "saturation": 0,
                   "texture": 16, "shadows": 15, "highlights": -24, "whites": 5, "blacks": -7,
                   "subject_lift": 0.14, "bg_control": -0.10, "skin_face": 0.0,
                   "nr_bias": 0.5, "sharp": 66, "canvas": 0},
    "commercial": {"contrast": 14, "clarity": 8, "vibrance": 8, "saturation": -1,
                   "texture": 10, "shadows": 16, "highlights": -20, "whites": 6, "blacks": -4,
                   "subject_lift": 0.10, "bg_control": 0.02, "skin_face": 0.0,
                   "nr_bias": 0.35, "sharp": 58, "canvas": 3},
    "general":    {"contrast": 16, "clarity": 9, "vibrance": 12, "saturation": 0,
                   "texture": 9, "shadows": 20, "highlights": -24, "whites": 5, "blacks": -5,
                   "subject_lift": 0.12, "bg_control": -0.04, "skin_face": 0.06,
                   "nr_bias": 0.4, "sharp": 55, "canvas": 4},
}

SKIN_TEMP_RANGE = (4300.0, 7600.0)
SAFE_FACE_LUMA = (0.47, 0.72)


@dataclass
class Recipe:
    global_: GlobalAdjust = field(default_factory=GlobalAdjust)
    masks: List[MaskLayer] = field(default_factory=list)
    explanation: List[ExplanationItem] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)
    denoise: Dict[str, Any] = field(default_factory=dict)
    sharpening: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    crop: Dict[str, Any] = field(default_factory=dict)
    targets: Dict[str, float] = field(default_factory=dict)

    def explain(self, area: str, action: str, reason: str, amount: str = "",
                confidence: float = 0.85) -> None:
        self.explanation.append(ExplanationItem(area=area, action=action, reason=reason,
                                               amount=amount, confidence=confidence))

    def mask(self, name: str, kind: str, adjustments: GlobalAdjust, *,
             confidence: float = 0.8, feather: float = 0.35, notes: str = "",
             geometry: Optional[Dict[str, Any]] = None,
             provenance: str = "ai") -> MaskLayer:
        layer = MaskLayer(name=name, kind=kind, adjustments=adjustments,
                          confidence=confidence, feather=feather, notes=notes,
                          geometry=geometry or {}, provenance=provenance)
        self.masks.append(layer)
        return layer


# ---------------------------------------------------------------------------- helpers
def _noise_iso(a: AnalysisResult) -> int:
    iso = int(a.quality.camera.get("iso") or 0)
    return max(iso, int(a.quality.iso_estimate or 0), 100)


def _noise_penalty(a: AnalysisResult) -> float:
    return float(clamp((a.quality.noise_sigma - 0.6) / 4.5, 0.0, 1.0))


def _highlight_risk(a: AnalysisResult) -> float:
    return float(clamp(a.exposure.clipping_high / 0.006 + max(0.0, -a.exposure.headroom_high) * 0.0, 0.0, 1.0))


def _contrast_number(char: str) -> float:
    return {"soft": 0.72, "balanced": 1.0, "punchy": 1.28, "editorial": 1.12}.get(char, 1.0)


def _colour_temp_bias(char: str) -> float:
    return {"warm": 180.0, "cool": -180.0, "editorial": 60.0, "vibrant": 30.0}.get(char, 0.0)


def _make_curve(intent: Intent) -> Optional[List[int]]:
    if intent.curve != "filmic":
        return None
    anchors = [(0.0, 0.012), (0.08, 0.075), (0.28, 0.276), (0.5, 0.505),
               (0.72, 0.735), (0.9, 0.917), (1.0, 0.995)]
    lut = color.curve_lut(anchors, size=256)
    return [int(round(v * 255)) for v in lut]


# ---------------------------------------------------------------------------- core build
def build_recipe(a: AnalysisResult, profile: ProfileSpec, intent_name: str,
                 strength: float = 1.0, quality: str = "high",
                 manual: Optional[Dict[str, Any]] = None) -> Recipe:
    intent = get_intent(intent_name)
    genre = a.genre if a.genre in GENRE_BASE else "general"
    base = dict(GENRE_BASE[genre])
    ex, col, q, sc = a.exposure, a.colour, a.quality, a.scene
    r = Recipe()
    g = r.global_
    noise = _noise_penalty(a)
    iso = _noise_iso(a)
    detail_keep = clamp(profile.detail_preservation, 0.0, 1.0)
    masc = 1.0 - 0.45 * noise            # a mask on contrast/detail when noisy
    # --- adapt to what the capture already has ------------------------------
    # A frame that already carries strong contrast or vivid colour must not be
    # pushed further just because its genre usually wants punch.
    in_contrast = float(a.exposure.p95 - a.exposure.p05)
    contrast_scale = float(np.clip(1.0 - (in_contrast - 0.52) / 0.30, 0.30, 1.20))
    in_sat = float(a.colour.saturation_p95)
    sat_scale_adapt = float(np.clip(1.0 - (in_sat - 0.55) / 0.35, 0.35, 1.15))
    in_skin_sat = float(a.colour.skin_saturation)

    # ---------------------------------------------------------------- exposure
    target_face = {"sports": 0.615, "wedding": 0.635, "portrait": 0.625, "event": 0.605,
                   "landscape": 0.600, "wildlife": 0.600, "commercial": 0.620}.get(genre, 0.615)
    target_face = float(clamp(profile.advanced.get("skin_target_luma", target_face), 0.45, 0.75))
    if sc.night > 0.5 or sc.artificial > 0.65:
        # A face lit to daylight-portrait luminance in a dark venue looks pasted
        # in.  Publishable night sport sits a little lower; keep it there.
        target_face -= 0.06
    cond = str(profile.advanced.get("condition", "") or "")
    if cond == "night" and sc.night < 0.4:
        r.warnings.append("Profile expects a night game but this frame looks like daylight; "
                          "adaptation reduced.")
    primary_faces = a.primary_faces()
    ref_luma = ex.face_luma if primary_faces else (ex.subject_luma if a.subject_confidence > 0.4
                                             else ex.median_luma)
    weighted = 0.72 * ref_luma + 0.28 * ex.median_luma
    target = target_face if primary_faces else max(target_face - 0.03, 0.46)
    ev = float(np.clip(np.log2((target + 1e-3) / (weighted + 1e-3)) * 0.72, -1.4, 1.6))
    if sc.night > 0.55:
        ev = min(ev, 0.35)
    hl = clamp(base["highlights"] * intent.highlights, -80, 10)
    if ex.clipping_high > 0.01 or ex.headroom_high < 0.25:
        hl = min(hl, -45.0)
    g.highlights = round(hl, 1)
    # Headroom guard: never push exposure past what the capture's highlight
    # distribution can absorb, allowing a little more when recovery is strong.
    p99 = float(a.exposure.p99)
    lin_white = float(color.srgb_to_linear(np.float32(0.985)))
    lin_p99 = float(color.srgb_to_linear(np.float32(np.clip(p99, 0.02, 0.999))))
    allowed = float(np.log2(lin_white / max(lin_p99, 1e-4)))
    if hl <= -35:
        allowed += 0.30
    ev = min(ev, max(allowed, -0.3))
    g.exposure = round(ev, 2)
    sh = clamp(base["shadows"] * intent.shadows, -10, 70)
    if sc.night > 0.5 and ev > 0:
        sh += 8.0
    if noise > 0.6:
        sh -= 8.0          # lifting noisy shadows would show grain
    g.shadows = round(clamp(sh, -10, 75), 1)
    g.whites = round(clamp(base["whites"] * intent.whites, -25, 30), 1)
    g.blacks = round(clamp(base["blacks"] + (4 if sc.overcast > 0.5 else 0) -
                           (3 if sc.night > 0.5 else 0), -35, 20), 1)
    g.contrast = round(clamp(base["contrast"] * intent.contrast * contrast_scale *
                             (1 - 0.22 * noise) * _contrast_number(profile.contrast_character),
                             -20, 55), 1)
    clarity_bias = float(profile.advanced.get("clarity_bias", 0.0))
    clarity_scale = float(np.clip(contrast_scale * 0.65 + 0.35, 0.45, 1.15))
    g.clarity = round(clamp(base["clarity"] * intent.clarity * masc * clarity_scale +
                            clarity_bias * 10, -20, 45), 1)
    g.texture = round(clamp(base["texture"] * intent.detail * masc * clarity_scale +
                            clarity_bias * 6, -20, 45), 1)
    g.dehaze = round(clamp(float(profile.advanced.get("dehaze_bias", 0.0)) * 10 +
                           (4.0 if cond == "outdoor" and sc.overcast > 0.45 else 0.0), -10, 18), 1)

    # ---------------------------------------------------------------- colour
    sat_scale = 1.0 + profile.saturation_bias + intent.saturation * (0.12 if intent_name != "automatic" else 0.0)
    vib = base["vibrance"] * intent.vibrance * sat_scale_adapt * (1.0 + profile.vibrance_bias)
    if col.cast_confidence < 0.2:
        vib *= 0.95
    if col.is_monochrome:
        vib = 0.0
    g.vibrance = round(clamp(vib * (1 - 0.35 * noise), -25, 50), 1)
    g.saturation = round(clamp((base["saturation"] * sat_scale + (intent.saturation - 1.0) * 8) *
                               sat_scale_adapt * (1 - 0.3 * noise), -20, 30), 1)
    if in_skin_sat > 0.55:
        # Already strongly saturated skin: hold back colour even further.
        g.vibrance = round(g.vibrance * 0.8, 1)
        g.saturation = round(g.saturation - 2, 1)

    # ---------------------------------------------------------------- white balance
    apply_wb = col.wb_confidence > 0.18 or col.cast_strength > 0.12
    if apply_wb:
        tol = float(profile.advanced.get("wb_tolerance_k", 520.0 if genre in ("sports", "event", "wedding") else 380.0))
        tol = float(clamp(tol, 120.0, 1200.0))
        temp = float(np.clip(col.temperature, *SKIN_TEMP_RANGE))
        target_temp = 0.5 * (temp + 6500.0) if col.wb_confidence < 0.5 else temp
        delta = temp - 6500.0
        shift = max(-tol, min(tol, delta)) * max(intent.wb_amount, 0.4)
        temp_bias = float(profile.advanced.get("temp_bias_k", 0.0))
        g.temperature = round(6500.0 + shift + intent.warmth + temp_bias +
                              _colour_temp_bias(profile.colour_character), -1)
        g.tint = round(float(np.clip(col.tint * 0.55, -25, 25)), 1)
        r.explain("White balance", "Adjusted capture white balance",
                  f"Measured cast of {col.cast_strength:.2f} with "
                  f"{'high' if col.wb_confidence > 0.5 else 'moderate'} confidence; "
                  "moved a fraction of the way to neutral to keep ambient character.",
                  amount=f"{int(g.temperature)}K / tint {g.tint:+.0f}")
    else:
        g.temperature = None
        g.tint = None
        r.explain("White balance", "Left as shot",
                  "No reliable neutral reference in this frame, so the captured "
                  "balance is preserved rather than guessed.", confidence=0.7)

    # ---------------------------------------------------------------- HSL
    g.hsl_hue, g.hsl_saturation, g.hsl_luminance = _hsl_for(a, profile, genre, intent)

    # ---------------------------------------------------------------- curve & finishing
    curve = _make_curve(intent)
    if curve:
        g.curve_rgb = curve
    vign = float(profile.vignette_amount or 0.0) * intent.vignette
    if a.subject_confidence > 0.45 and genre in ("sports", "event", "portrait", "wedding"):
        vign -= 3.0 * intent.vignette          # gentle subject separation only
    g.post_crop_vignette = round(clamp(vign, -12, 0), 1)
    g.grain = round(clamp((profile.grain_amount or 0.0) + intent.grain * 22, 0, 45), 1)

    # ---------------------------------------------------------------- masks
    r.targets = {"face_luma": target_face,
                 "contrast": {"natural": 0.44, "automatic": 0.50, "vibrant": 0.55,
                              "dramatic": 0.60, "editorial": 0.55}.get(intent_name, 0.50),
                 "saturation": {"natural": 0.24, "automatic": 0.28, "vibrant": 0.33,
                                "dramatic": 0.28, "editorial": 0.27}.get(intent_name, 0.28)}
    _build_masks(r, a, profile, genre, intent, base)

    # ---------------------------------------------------------------- detail
    nrd = clamp(0.5 * noise + base["nr_bias"] * 0.35 + profile.noise_reduction_bias * 0.25, 0, 1)
    g.luminance_nr = round(clamp(0 + 62 * nrd, 0, 85), 1)
    g.color_nr = round(clamp(25 + 55 * clamp(noise + 0.15, 0, 1), 20, 90), 1)
    g.luminance_detail = round(clamp(80 - 45 * nrd + 20 * detail_keep, 25, 95), 1)
    g.color_detail = 50.0
    sharp_base = base["sharp"] * (0.85 + 0.3 * profile.sharpening_bias)
    sharp_base *= (1 - 0.28 * noise) * (0.9 + 0.2 * detail_keep)
    if a.quality.sharpness < 0.35:
        sharp_base += 6
    if a.quality.motion_blur > 0.6:
        sharp_base -= 6
    g.sharpness = round(clamp(sharp_base, 30, 95), 1)
    g.sharpen_radius = 1.0 if max(a.width, a.height) > 1600 else 0.8
    g.sharpen_detail = round(clamp(22 + 22 * detail_keep, 15, 50), 1)
    g.sharpen_masking = round(clamp(18 + 52 * noise + (22 if primary_faces else 0), 0, 92), 1)
    psf = float(clamp(1.25 - 0.55 * (a.quality.sharpness - 0.35), 0.7, 1.5))
    r.sharpening = {"deconvolve": a.quality.sharpness < 0.5 and noise < 0.55,
                    "psf_sigma": round(psf, 2),
                    "iterations": 6 if quality != "standard" else 4}
    r.denoise = {"sigma": round(a.quality.noise_sigma / 100.0, 4),
                 "iso": iso, "method": "guided-base/detail split" if quality != "standard"
                 else "guided chroma + detail-safe luma"}
    r.detail = {
        "noise_sigma": round(a.quality.noise_sigma / 100.0, 4),
        "skin_protection": round(clamp(profile.skin_protection, 0, 1), 2),
        "deconvolution": round(clamp(0.42 * clamp(0.6 - a.quality.sharpness, 0, 0.6) *
                                     (1 - noise), 0, 0.35), 3) if quality == "maximum" else 0.0,
        "subject_emphasis": base["subject_lift"],
    }
    g.lens_corrections = True
    g.remove_ca = True
    g.defringe = round(clamp((a.quality.lens.get("ca", 0) - 0.25) * 40, 0, 20), 1)

    # ---------------------------------------------------------------- crop suggestion
    if profile.crop_suggestion:
        r.crop = suggest_crop(a)

    # ---------------------------------------------------------------- limits & explanation
    _apply_safety(r, a, genre, intent)
    _explain_global(r, a, genre, noise)
    if noise > 0.55:
        r.warnings.append(f"High-ISO frame (~ISO {iso}): detail lift bounded to protect the noise floor.")
    if a.subject_confidence < 0.35:
        r.warnings.append("Subject separation uncertain: local adjustments kept gentle and global.")
    return r


# ---------------------------------------------------------------------------- HSL
def _hsl_for(a: AnalysisResult, profile: ProfileSpec, genre: str, intent: Intent):
    hue: Dict[str, float] = {}
    sat: Dict[str, float] = {}
    lum: Dict[str, float] = {}
    regions = a.regions
    grass_cov = regions.get("grass").coverage if regions.get("grass") else 0.0
    sky_cov = regions.get("sky").coverage if regions.get("sky") else 0.0
    court_cov = regions.get("court").coverage if regions.get("court") else 0.0

    grass_cov = max(grass_cov, a.scene.grass_coverage)
    sky_cov = max(sky_cov, a.scene.sky_coverage)
    court_cov = max(court_cov, a.scene.court_coverage)

    grass_strength = clamp(profile.grass_enhancement, 0, 1) * (1.0 if grass_cov > 0.12 else 0.0)
    if grass_strength > 0.05:
        # Nudge grass hue slightly toward true green (Lightroom green channel is 120°).
        sat["green"] = round(7.5 * grass_strength, 1)
        sat["yellow"] = round(3.0 * grass_strength, 1)
        hue["green"] = round(-3.0 * grass_strength, 1)
        lum["green"] = round(-1.5 * grass_strength, 1)

    sky_strength = clamp(profile.sky_enhancement, 0, 1) * (1.0 if sky_cov > 0.05 else 0.0)
    if sky_strength > 0.05:
        sat["blue"] = round(8.0 * sky_strength, 1)
        sat["aqua"] = round(4.0 * sky_strength, 1)
        lum["blue"] = round(-5.0 * sky_strength, 1)
        hue["blue"] = round(2.0 * sky_strength, 1)

    # Skin: hold saturation, lift luminance slightly — *never* shift hue.
    if a.primary_faces():
        lum["orange"] = round(2.5 * clamp(profile.skin_protection, 0, 1), 1)
        sat["orange"] = round(-1.5 * clamp(profile.skin_protection, 0, 1), 1)

    # Uniform accuracy: keep saturated reds/super-saturated kit from blocking up.
    if genre in ("sports", "event") and clamp(profile.uniform_accuracy, 0, 1) > 0.2:
        sat["red"] = round(-3.0 * clamp(profile.uniform_accuracy, 0, 1), 1)
        lum["red"] = round(1.5 * clamp(profile.uniform_accuracy, 0, 1), 1)
    if court_cov > 0.05:
        lum["orange"] = round(-2.0 * intent.detail, 1)
    grass_boost = float(profile.advanced.get("grass_boost", 0.0))
    if grass_boost and grass_cov > 0.05:
        sat["green"] = round(sat.get("green", 0.0) + grass_boost * 8, 1)
        hue["green"] = round(hue.get("green", 0.0) - grass_boost * 3, 1)
    overrides = profile.advanced.get("hsl_overrides") or {}
    for channel, vals in overrides.items():
        if not isinstance(vals, dict):
            continue
        if "hue" in vals:
            hue[str(channel)] = float(np.clip(vals["hue"], -40, 40))
        if "saturation" in vals:
            sat[str(channel)] = float(np.clip(vals["saturation"], -50, 50))
        if "luminance" in vals:
            lum[str(channel)] = float(np.clip(vals["luminance"], -50, 50))
    return hue, sat, lum


# ---------------------------------------------------------------------------- masks
def _has(a: AnalysisResult, kind: str, min_cov: float = 0.0) -> bool:
    reg = a.regions.get(kind)
    if reg is None:
        return kind in ("subject", "background") and a.subject_confidence > 0.3 and min_cov == 0.0
    return reg.coverage >= min_cov and reg.confidence >= 0.25


def _build_masks(r: Recipe, a: AnalysisResult, profile: ProfileSpec, genre: str,
                 intent: Intent, base: Dict[str, float]) -> None:
    subject_ok = a.subject_confidence > 0.34 and a.subject_coverage > 0.02
    has_faces = bool(a.primary_faces())
    subject_boost = float(base["subject_lift"]) * (0.6 + 0.7 * clamp(profile.subject_emphasis, 0, 1.2))
    bg_control = float(base["bg_control"]) * (0.5 + 0.9 * clamp(profile.background_control, 0, 1.2))
    skin_face = float(base["skin_face"]) * clamp(profile.skin_protection, 0, 1)
    face_luma = a.exposure.face_luma if has_faces else a.exposure.subject_luma
    has_faces = bool(a.primary_faces())

    # ---- subject separation
    if subject_ok and abs(subject_boost) > 0.02:
        adj = GlobalAdjust(
            exposure=round(subject_boost, 2),
            contrast=round(6 * clamp(profile.subject_emphasis, 0, 1.2), 1) if genre in ("sports", "event") else 0.0,
            clarity=round(4 * clamp(profile.subject_emphasis, 0, 1.2), 1) if genre in ("sports", "wildlife") else 0.0,
            texture=round(3.0 * clamp(profile.detail_preservation, 0, 1), 1),
            shadows=round(4.0 if genre in ("sports", "event") else 2.0, 1),
            sharpness=round(40 + 18 * clamp(profile.sharpening_bias, 0, 1), 1) if genre in ("sports", "wildlife") else 40.0,
            luminance_nr=12.0,
        )
        r.mask("Subject", "subject", adj, confidence=a.subject_confidence, feather=0.4,
               notes="Separates the athlete/subject without touching the background exposure.")
        r.explain("Subject", "Lifted subject over background",
                  "Face/subject luminance measured below the genre target; a masked "
                  "lift adds presence without brightening the venue.",
                  amount=f"{subject_boost:+.2f} EV", confidence=a.subject_confidence)

    if subject_ok and abs(bg_control) > 0.02:
        adj = GlobalAdjust(
            exposure=round(bg_control, 2),
            contrast=round(-4.0, 1) if genre in ("sports", "portrait", "wedding") else 0.0,
            saturation=round(-3.0, 1) if genre in ("sports", "wedding", "event") else 0.0,
            vibrance=round(-4.0, 1) if genre == "sports" else 0.0,
            clarity=round(-3.0, 1) if genre in ("portrait", "wedding") else 0.0,
            highlights=round(-6.0, 1) if a.exposure.clipping_high > 0.001 else 0.0,
            sharpness=round(40 - 8.0, 1),
            luminance_nr=12.0,
        )
        r.mask("Background", "background", adj, confidence=a.subject_confidence, feather=0.5,
               notes="Calms and separates the background from the subject.")

    # ---- face / skin (the most important local corrections in people photography)
    if has_faces:
        face_target = r.targets.get("face_luma", 0.615) if r.targets else 0.615
        face_ev = float(clamp(np.log2((face_target + 1e-3) / (max(face_luma, 0.05) + 1e-3)) * 0.30,
                              -0.45, 0.55))
        if abs(face_ev) > 0.03:
            adj = GlobalAdjust(exposure=round(face_ev, 2),
                               shadows=round(max(0.0, face_ev * 18), 1) if a.exposure.face_luma < 0.42 else 0.0,
                               contrast=round(3.0, 1) if genre in ("sports",) else 0.0,
                               saturation=0.0, temperature=None)
            r.mask("Faces", "face", adj, confidence=0.85, feather=0.45,
                   notes="Balances facial exposure only — never alters facial structure.")
            r.explain("Faces", "Balanced facial exposure",
                      f"Faces measured L={face_luma:.2f} against a {genre} target of "
                      f"{face_target:.2f}; corrected locally so the venue stays natural.",
                      amount=f"{face_ev:+.2f} EV")

        skin_adj = GlobalAdjust(saturation=round(-1.0 * skin_face * 10, 1),
                                vibrance=round(-1.5 * skin_face * 10, 1),
                                luminance_nr=8.0, texture=round(-2.0 * skin_face, 1),
                                sharpness=round(34.0, 1))
        r.mask("Skin", "skin", skin_adj, confidence=0.8, feather=0.6,
               notes="Protects skin micro-texture and colour; sharpening is deliberately soft.")

        # Eyes: crisp detail only where it reads as intentional.
        prim = a.primary_faces()
        if prim and max(f.w for f in prim) > 0.03 and genre in ("sports", "portrait", "wedding", "event"):
            eyes = GlobalAdjust(sharpness=round(40 + 26 * clamp(profile.detail_preservation, 0, 1), 1),
                                texture=4.0, clarity=2.0)
            r.mask("Eyes", "eyes", eyes, confidence=0.7, feather=0.2,
                   geometry={"faces": [f.to_dict() for f in prim[:4]]},
                   notes="Detail recovery on eyes only.")

    # ---- landscape / sky
    if genre in ("landscape", "wildlife") and _has(a, "sky", 0.05):
        sky = a.regions.get("sky")
        darkening = -10.0 * clamp(profile.sky_enhancement, 0, 1) - 4.0
        adj = GlobalAdjust(exposure=round(darkening / 100.0 * 1.1, 2),
                           highlights=round(-18.0, 1),
                           shadows=round(4.0, 1), clarity=round(4.0, 1),
                           vibrance=round(6.0, 1),
                           saturation=round(3.0, 1) if (sky and sky.mean_lab[1] < 6) else 0.0)
        r.mask("Sky", "sky", adj, confidence=sky.confidence if sky else 0.5, feather=0.55,
               notes="Separates sky from land; recovers cloud detail without halos.")
        r.explain("Sky", "Recovered sky detail",
                  "Sky occupies a large part of the frame and holds recoverable "
                  "highlight data in the cloud structure.",
                  amount=f"{adj.highlights:+.0f} highlights")

    # ---- sports surfaces & kit
    if genre in ("sports", "event", "general"):
        grass = a.regions.get("grass")
        grass_cov = max(grass.coverage if grass else 0.0, a.scene.grass_coverage)
        if grass_cov > 0.06 and clamp(profile.grass_enhancement, 0, 1) > 0.15:
            strength = clamp(profile.grass_enhancement, 0, 1)
            adj = GlobalAdjust(saturation=round(5.0 * strength, 1),
                               vibrance=round(4.0 * strength, 1),
                               exposure=round(-0.03 * strength, 2),
                               clarity=round(3.0 * strength, 1),
                               luminance_nr=8.0, sharpness=round(38.0, 1))
            r.mask("Playing surface", "grass", adj, confidence=0.75, feather=0.5,
                   notes="Keeps grass rich and natural; hue is nudged, never replaced.")
            r.explain("Playing surface", "Balanced grass colour",
                      "Grass is a reference colour for viewers: saturation lifted only "
                      "enough to read as healthy turf, with luminance held constant.",
                      amount=f"+{adj.saturation:.0f} sat")

        court = a.regions.get("court")
        if court and court.coverage > 0.06 and genre == "sports":
            adj = GlobalAdjust(saturation=round(3.0, 1), exposure=round(-0.02, 2),
                               clarity=round(2.0, 1), sharpness=38.0)
            r.mask("Court", "court", adj, confidence=0.7, feather=0.5,
                   notes="Hardwood/painted court kept neutral and consistent.")

        if _has(a, "clothing", 0.01):
            adj = GlobalAdjust(saturation=round(3.0 * clamp(profile.uniform_accuracy, 0, 1), 1),
                               vibrance=round(2.0, 1), texture=3.0,
                               sharpness=round(40 + 8 * clamp(profile.detail_preservation, 0, 1), 1))
            r.mask("Uniform / clothing", "clothing", adj, confidence=0.65, feather=0.55,
                   notes="Keeps kit colours accurate; no hue shifts on team colours.")

    # ---- backgrounds: crowd + distractions
    if genre in ("sports", "event", "wedding") and a.subject_confidence > 0.4:
        crowd = a.regions.get("crowd")
        crowd_cov = max(crowd.coverage if crowd else 0.0, a.scene.crowd_coverage)
        control = clamp(profile.background_control, 0, 1)
        control = clamp(control + float(profile.advanced.get("crowd_control", 0.0)), 0, 1.2)
        if crowd_cov > 0.04 and control > 0.15:
            adj = GlobalAdjust(exposure=round(-0.10 - 0.08 * control, 2),
                               saturation=round(-4.0 - 4.0 * control, 1),
                               clarity=round(-5.0 * control, 1),
                               texture=round(-3.0, 1),
                               luminance_nr=18.0)
            r.mask("Spectators / background distraction", "crowd", adj,
                   confidence=0.6, feather=0.6,
                   notes="Quietens busy spectator areas so the subject holds attention.")
            r.explain("Spectators", "Reduced background distraction",
                      "Heavily textured spectator area competes with the subject for "
                      "attention; tone and texture were pulled back locally.",
                      amount=f"{adj.exposure:+.2f} EV")

    # ---- mixed lighting correction
    if a.scene.mixed_light > 0.35 or (a.scene.artificial > 0.55 and genre in ("event", "wedding", "sports")):
        wl = a.regions.get("warm_light")
        cl = a.regions.get("cool_light")
        if wl and wl.coverage > 0.01:
            r.mask("Warm light pools", "warm_light",
                   GlobalAdjust(temperature=round(6500 - 420, -1), tint=-3.0, saturation=-2.0),
                   confidence=0.55, feather=0.7,
                   notes="Neutralises tungsten/stage pools on the background.")
            r.explain("Mixed lighting", "Neutralised warm light pools",
                      "White balance differs across the frame (mixed sources); the warm "
                      "regions were corrected locally instead of biasing the whole image.",
                      confidence=0.6)
        if cl and cl.coverage > 0.01:
            r.mask("Cool light pools", "cool_light",
                   GlobalAdjust(temperature=round(6500 + 380, -1), tint=2.0, saturation=-2.0),
                   confidence=0.55, feather=0.7,
                   notes="Neutralises daylight spill and LED pools on the background.")

    # ---- wedding / portrait specific
    if genre == "wedding":
        if a.exposure.clipping_high > 0.002 or a.exposure.p95 > 0.95:
            adj = GlobalAdjust(highlights=round(-16.0, 1), whites=round(-4.0, 1),
                               texture=round(5.0, 1), clarity=2.0, sharpness=46.0)
            r.mask("Dress detail", "highlight_detail", adj, confidence=0.6, feather=0.5,
                   notes="Recovers fabric detail in bright whites without flattening them.")
            r.explain("Dress detail", "Protected dress whites",
                      "Bright fabric clipping at the highlights; local recovery preserves "
                      "lace and fold detail.", amount=f"{adj.highlights:+.0f}")
    if genre in ("portrait", "wedding", "commercial"):
        if subject_ok and clamp(profile.background_control, 0, 1) > 0.2:
            r.mask("Background softening", "background",
                   GlobalAdjust(clarity=round(-6.0 * clamp(profile.background_control, 0, 1), 1),
                                texture=round(-4.0, 1), sharpness=38.0),
                   confidence=0.55, feather=0.6,
                   notes="Subtle background falloff for subject separation.")

    # ---- landscape atmosphere
    if genre == "landscape" and a.scene.sky_coverage > 0.08 and a.scene.overcast < 0.6:
        bands = []
        for i, (y0, y1, amt) in enumerate(((0.0, 0.18, 0.06), (0.18, 0.38, 0.03))):
            bands.append(("Atmospheric depth", "linear",
                          GlobalAdjust(exposure=-amt, clarity=1.0, dehaze=3.0),
                          {"x0": 0.0, "y0": y0, "x1": 0.0, "y1": y1}))
        for name, kind, adj, geom in bands:
            r.mask(name, kind, adj, confidence=0.5, feather=0.9, geometry=geom,
                   notes="Gentle luminance gradient to deepen the sky naturally.")


# ---------------------------------------------------------------------------- crop
def suggest_crop(a: AnalysisResult) -> Dict[str, Any]:
    """Crop suggestion from face position, subject placement and horizon tilt."""
    out: Dict[str, Any] = {"available": False}
    if a.faces:
        f = max(a.faces, key=lambda z: z.area)
        cx, cy = f.center
        out.update({"available": True, "kind": "rule-of-thirds",
                    "subject": [round(cx, 3), round(cy, 3)],
                    "suggestion": "Place the dominant face on a third intersection"
                    if not (0.30 < cx < 0.45 or 0.55 < cx < 0.70) else "Composition already close to thirds",
                    "confidence": 0.55})
    elif a.subject_bbox:
        bx, by, bw, bh = a.subject_bbox
        cx = bx + bw / 2
        out.update({"available": True, "kind": "subject-centred",
                    "subject": [round(cx, 3), round(by + bh / 2, 3)],
                    "suggestion": "Subject centre is off-centre; a modest crop would balance the frame"
                    if abs(cx - 0.5) > 0.1 else "Subject is well centred",
                    "confidence": 0.4})
    tilt = a.scene.horizon_tilt
    if abs(tilt) > 0.6:
        out.update({"straighten": round(float(np.clip(tilt, -12, 12)), 2),
                    "available": True,
                    "note": f"Horizon appears tilted by {tilt:+.1f}°"})
    return out


# ---------------------------------------------------------------------------- safety
def _apply_safety(r: Recipe, a: AnalysisResult, genre: str, intent: Intent) -> None:
    g = r.global_
    # Never let a genre push skin into unnatural brightness or darkness.
    if a.primary_faces():
        if a.exposure.face_luma > 0.80 and g.exposure > 0:
            g.exposure = 0.0
            r.warnings.append("Faces already bright: exposure lift suppressed.")
        if a.exposure.face_luma < 0.18 and g.exposure < 0 and a.scene.night < 0.6:
            g.exposure = max(g.exposure, 0.2)
            r.warnings.append("Faces underexposed: exposure lift enforced.")
    # Contrast is not allowed to crush an already thin histogram.
    if a.exposure.p95 - a.exposure.p05 < 0.22 and g.contrast > 18:
        g.contrast = 18.0
        r.warnings.append("Low-contrast capture: contrast capped to avoid posterising.")
    if g.grain > 0 and a.quality.noise_sigma > 2.5:
        g.grain = 0.0
    if not a.quality.is_raw and g.dehaze > 0:
        g.dehaze = min(g.dehaze, 8.0)
    # Sharpening must never run away on a soft, high-ISO file.
    g.sharpness = float(np.clip(g.sharpness, 25, 95))
    g.luminance_nr = float(np.clip(g.luminance_nr, 0, 90))


def _explain_global(r: Recipe, a: AnalysisResult, genre: str, noise: float) -> None:
    g = r.global_
    r.explain("Exposure", "Set base exposure",
              f"Face/subject luminance {a.exposure.face_luma if a.faces else a.exposure.subject_luma:.2f} "
              f"against a {genre} target; histogram headroom "
              f"{a.exposure.headroom_high:.2f} stops.",
              amount=f"{g.exposure:+.2f} EV")
    r.explain("Tone", "Sculpted highlights and shadows",
              "Highlights are pulled to hold detail in bright kit/clouds; shadows are "
              "opened just enough to read fabric and faces without flattening the lighting.",
              amount=f"H {g.highlights:+.0f} / S {g.shadows:+.0f}")
    r.explain("Contrast & clarity", "Added controlled local presence",
              "Contrast and local contrast are scaled to the noise floor and genre so "
              "the frame gains depth without halos or crunch.",
              amount=f"C {g.contrast:+.0f} / Cl {g.clarity:+.0f}")
    r.explain("Colour", "Balanced vibrance and targeted HSL",
              "Vibrance carries most of the colour lift (low-saturation pixels first) "
              "while skin and uniform hues are held accurate.",
              amount=f"Vib {g.vibrance:+.0f} / Sat {g.saturation:+.0f}")
    r.explain("Detail", "Noise reduction then multi-scale sharpening",
              f"Measured noise {a.quality.noise_sigma / 100:.3f}, sharpness "
              f"{a.quality.sharpness:.2f}; noise reduction runs before sharpening with "
              "edge masking so detail is raised only where it is real.",
              amount=f"NR {g.luminance_nr:.0f} / Sharp {g.sharpness:.0f}")
    if genre == "sports":
        r.explain("Sports look", "Applied a publication-style treatment",
                  "Strong but controlled contrast, accurate kit colour, rich natural "
                  "turf and clean separation between athlete and venue.")
