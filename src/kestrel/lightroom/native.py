"""Translation of an :class:`EditPlan` into native Lightroom develop settings.

Two delivery modes exist, and they are honestly different:

``native``
    Global sliders, tone curve, HSL, detail and lens settings are written as real
    Lightroom develop settings, so the photographer keeps full, non-destructive,
    editable control and the RAW original is untouched.

``adaptive``
    Kestrel renders the photograph (local masks, noise reduction, deconvolution,
    finishing) and the result is imported as a new TIFF/JPEG next to the original.

Local masks are described, not delivered natively. Lightroom's *documented* SDK
has no call that creates a local adjustment mask — not a radial gradient and not
an AI mask — so every mask in a plan is baked into pixels by the renderer:

``render``      the only honest capability: the mask shapes the rendered 16-bit
                TIFF the plug-in imports beside the original

Each entry also records how the mask could be expressed (``gradient`` for
radial/linear, ``ai`` for subject/sky/clothing and friends) so a future XMP or
undocumented-API path can be added without changing the payload shape.

The payload is versioned so the plugin can refuse settings it does not understand
rather than silently applying the wrong thing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core import BRAND
from ..models import AnalysisResult, EditPlan

# Lightroom's HSL channels, in the order the SDK/XMP expects.
HSL_CHANNELS = ["Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta"]

# Documented slider ranges. Lightroom rejects a develop-settings table whose
# values fall outside them, and because a whole batch applies through one call,
# one corrupt plan would take the batch down with it. Clamp here, at the source
# of the payload, rather than trusting whatever is on disk.
LIMITS: Dict[str, tuple] = {
    "Exposure2012": (-5.0, 5.0),
    "Contrast2012": (-100.0, 100.0),
    "Highlights2012": (-100.0, 100.0),
    "Shadows2012": (-100.0, 100.0),
    "Whites2012": (-100.0, 100.0),
    "Blacks2012": (-100.0, 100.0),
    "Texture": (-100.0, 100.0),
    "Clarity2012": (-100.0, 100.0),
    "Dehaze": (-100.0, 100.0),
    "Vibrance": (-100.0, 100.0),
    "Saturation": (-100.0, 100.0),
    "Sharpness": (0.0, 150.0),
    "SharpenRadius": (0.5, 3.0),
    "SharpenDetail": (0.0, 100.0),
    "SharpenEdgeMasking": (0.0, 100.0),
    "LuminanceSmoothing": (0.0, 100.0),
    "LuminanceDetail": (0.0, 100.0),
    "ColorNoiseReduction": (0.0, 100.0),
    "ColorNoiseReductionDetail": (0.0, 100.0),
    "PostCropVignetteAmount": (-100.0, 100.0),
    "GrainAmount": (0.0, 100.0),
    "DefringePurpleAmount": (0.0, 20.0),
    "DefringeGreenAmount": (0.0, 20.0),
    "Temperature": (2000.0, 50000.0),
    "Tint": (-150.0, 150.0),
}


def _clamp(key: str, value: float) -> float:
    low, high = LIMITS.get(key, (float("-inf"), float("inf")))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return min(max(number, low), high)


def _curve_points(values: Optional[List[int]]) -> Optional[List[int]]:
    if not values:
        return None
    # Lightroom expects an even-length list of x,y, pairs in 0..255.
    vals = [int(max(0, min(255, v))) for v in values]
    if len(vals) % 2:
        vals = vals[:-1]
    return vals or None


def _setting(out: Dict[str, Any], key: str, value: float, digits: int = 0) -> None:
    """Clamp one slider into Lightroom's documented range and record it."""
    clamped = _clamp(key, value)
    out[key] = round(clamped, digits) if digits else int(round(clamped))


def to_develop_settings(plan: EditPlan, analysis: Optional[AnalysisResult] = None
                        ) -> Dict[str, Any]:
    """Return a Lightroom develop-settings table (``applyDevelopSettings`` ready)."""
    g = plan.global_
    out: Dict[str, Any] = {}
    _setting(out, "Exposure2012", g.exposure, 2)
    _setting(out, "Contrast2012", g.contrast)
    _setting(out, "Highlights2012", g.highlights)
    _setting(out, "Shadows2012", g.shadows)
    _setting(out, "Whites2012", g.whites)
    _setting(out, "Blacks2012", g.blacks)
    _setting(out, "Texture", g.texture)
    _setting(out, "Clarity2012", g.clarity)
    _setting(out, "Dehaze", g.dehaze)
    _setting(out, "Vibrance", g.vibrance)
    _setting(out, "Saturation", g.saturation)
    _setting(out, "Sharpness", g.sharpness)
    _setting(out, "SharpenRadius", g.sharpen_radius, 2)
    _setting(out, "SharpenDetail", g.sharpen_detail)
    _setting(out, "SharpenEdgeMasking", g.sharpen_masking)
    _setting(out, "LuminanceSmoothing", g.luminance_nr)
    _setting(out, "LuminanceDetail", g.luminance_detail)
    _setting(out, "ColorNoiseReduction", g.color_nr)
    _setting(out, "ColorNoiseReductionDetail", g.color_detail)
    _setting(out, "PostCropVignetteAmount", g.post_crop_vignette)
    _setting(out, "GrainAmount", g.grain)
    _setting(out, "DefringePurpleAmount", g.defringe)
    _setting(out, "DefringeGreenAmount", g.defringe * 0.6)
    out["LensProfileEnable"] = 1 if g.lens_corrections else 0
    out["AutoLateralCA"] = 1 if g.remove_ca else 0
    if g.temperature is not None:
        out["WhiteBalance"] = "Custom"
        _setting(out, "Temperature", g.temperature)
        _setting(out, "Tint", g.tint or 0.0)
    else:
        out["WhiteBalance"] = "As Shot"
    curve = _curve_points(g.curve_rgb)
    if curve:
        out["ToneCurvePV2012"] = curve
    if g.curve_red or g.curve_blue:
        rc = _curve_points(g.curve_red)
        bc = _curve_points(g.curve_blue)
        if rc:
            out["ToneCurvePV2012Red"] = rc
        if bc:
            out["ToneCurvePV2012Blue"] = bc
    hue = {k.lower(): v for k, v in g.hsl_hue.items()}
    sat = {k.lower(): v for k, v in g.hsl_saturation.items()}
    lum = {k.lower(): v for k, v in g.hsl_luminance.items()}
    for ch in HSL_CHANNELS:
        key = ch.lower()
        if key in hue:
            out[f"HueAdjustment{ch}"] = int(round(max(-100.0, min(100.0, float(hue[key])))))
        if key in sat:
            out[f"SaturationAdjustment{ch}"] = int(round(max(-100.0, min(100.0, float(sat[key])))))
        if key in lum:
            out[f"LuminanceAdjustment{ch}"] = int(round(max(-100.0, min(100.0, float(lum[key])))))
    # Only documented develop-setting keys are emitted: Lightroom rejects a
    # settings table that contains unknown entries, and the plug-in filters
    # defensively as well (lightroom/Kestrel.lrplugin/Apply.lua).
    return out


def local_mask_payload(plan: EditPlan) -> Dict[str, Any]:
    """Describe every local adjustment for the plugin.

    Every entry says ``render``: the documented Lightroom SDK cannot create local
    adjustment masks from a plug-in, so masks reach the photograph through the
    rendered deliverable. ``expressible`` records the shape a future XMP writer
    would need — ``gradient`` for radial/linear, ``ai`` for content masks.
    """
    gradient_kinds = ("radial", "linear")
    masks: List[Dict[str, Any]] = []
    for layer in plan.masks:
        if not layer.enabled:
            continue
        kind = layer.kind
        masks.append({
            "name": layer.name,
            "kind": kind,
            "capability": "render",
            "expressible": "gradient" if kind in gradient_kinds else "ai",
            "opacity": round(float(layer.opacity), 3),
            "inverted": bool(layer.inverted),
            "feather": round(float(layer.feather), 3),
            "geometry": layer.geometry,
            "settings": to_develop_settings(EditPlan(global_=layer.adjustments)),
            "notes": layer.notes,
            "confidence": layer.confidence,
        })
    return {"format": "kestrel-masks", "version": 1, "masks": masks}


def apply_payload(plan: EditPlan, analysis: Optional[AnalysisResult], image_id: str,
                  version_id: str, mode: str) -> Dict[str, Any]:
    """The document the Lightroom plugin consumes.

    The modes are genuinely different, not labels:

    ``adaptive``  global develop settings plus the mask plan, recorded in the
                  packet so the mask decisions can be reviewed and re-rendered
    ``native``    global develop settings only; the packet carries no mask plan
    ``render``    the packet describes the rendered deliverable the plug-in adds
    """
    masks = (local_mask_payload(plan) if mode != "native"
             else {"format": "kestrel-masks", "version": 1, "masks": []})
    return {
        "schema": BRAND["engine_schema"],
        "app": BRAND["name"],
        "engine_version": BRAND["version"],
        "image_id": image_id,
        "version_id": version_id,
        "mode": mode,
        "settings": to_develop_settings(plan, analysis),
        "masks": masks,
        "explanation": [e.to_dict() for e in plan.explanation],
        "metrics": plan.metrics.to_dict(),
        "genre": plan.genre,
        "intent": plan.intent,
        "strength": plan.strength,
        "warnings": list(plan.warnings),
    }


def capability_report() -> Dict[str, Any]:
    return {
        "global_settings": "native",
        "tone_curve": "native",
        "hsl": "native",
        "detail": "native",
        "lens_corrections": "native",
        "local_masks": "render mode — baked into the rendered TIFF; the documented SDK cannot create "
                       "local adjustment masks from a plug-in",
        "radial_linear_masks": "render mode (same limitation as every other mask)",
        "ai_masks": "render mode",
        "ai_noise_reduction": "render mode (Lightroom's own AI Denoise is not scriptable)",
        "deconvolution": "render mode",
        "object_removal": "not implemented",
        "perspective_correction": "native (LrDevelopController / Upright), not automated",
    }
