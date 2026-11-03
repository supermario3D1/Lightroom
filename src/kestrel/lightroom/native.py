"""Translation of an :class:`EditPlan` into native Lightroom develop settings.

Two delivery modes exist, and they are honestly different:

``native``
    Global sliders, tone curve, HSL, detail and lens settings are written as real
    Lightroom develop settings, so the photographer keeps full, non-destructive,
    editable control and the RAW original is untouched.

``adaptive``
    Kestrel renders the photograph (local masks, noise reduction, deconvolution,
    finishing) and the result is imported as a new TIFF/JPEG next to the original.

Local masks are classified per capability:

``native``      geometric gradients (radial/linear) — portable XMP support
``plugin``      AI/soft masks applied through the Lightroom SDK during the session
``render``      detail-critical work that only the full renderer can do

The payload is versioned so the plugin can refuse settings it does not understand
rather than silently applying the wrong thing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core import BRAND
from ..models import AnalysisResult, EditPlan

# Lightroom's HSL channels, in the order the SDK/XMP expects.
HSL_CHANNELS = ["Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta"]


def _curve_points(values: Optional[List[int]]) -> Optional[List[int]]:
    if not values:
        return None
    # Lightroom expects an even-length list of x,y, pairs in 0..255.
    vals = [int(max(0, min(255, v))) for v in values]
    if len(vals) % 2:
        vals = vals[:-1]
    return vals or None


def to_develop_settings(plan: EditPlan, analysis: Optional[AnalysisResult] = None
                        ) -> Dict[str, Any]:
    """Return a Lightroom develop-settings table (``applyDevelopSettings`` ready)."""
    g = plan.global_
    out: Dict[str, Any] = {
        "Exposure2012": round(float(g.exposure), 2),
        "Contrast2012": round(float(g.contrast), 0),
        "Highlights2012": round(float(g.highlights), 0),
        "Shadows2012": round(float(g.shadows), 0),
        "Whites2012": round(float(g.whites), 0),
        "Blacks2012": round(float(g.blacks), 0),
        "Texture": round(float(g.texture), 0),
        "Clarity2012": round(float(g.clarity), 0),
        "Dehaze": round(float(g.dehaze), 0),
        "Vibrance": round(float(g.vibrance), 0),
        "Saturation": round(float(g.saturation), 0),
        "Sharpness": round(float(g.sharpness), 0),
        "SharpenRadius": round(float(g.sharpen_radius), 1),
        "SharpenDetail": round(float(g.sharpen_detail), 0),
        "SharpenEdgeMasking": round(float(g.sharpen_masking), 0),
        "LuminanceSmoothing": round(float(g.luminance_nr), 0),
        "LuminanceDetail": round(float(g.luminance_detail), 0),
        "ColorNoiseReduction": round(float(g.color_nr), 0),
        "ColorNoiseReductionDetail": round(float(g.color_detail), 0),
        "PostCropVignetteAmount": round(float(g.post_crop_vignette), 0),
        "GrainAmount": round(float(g.grain), 0),
        "LensProfileEnable": 1 if g.lens_corrections else 0,
        "AutoLateralCA": 1 if g.remove_ca else 0,
        "DefringePurpleAmount": round(float(g.defringe), 0),
        "DefringeGreenAmount": round(float(g.defringe * 0.6), 0),
    }
    if g.temperature is not None:
        out["WhiteBalance"] = "Custom"
        out["Temperature"] = int(round(float(g.temperature)))
        out["Tint"] = int(round(float(g.tint or 0.0)))
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
            out[f"HueAdjustment{ch}"] = int(round(hue[key]))
        if key in sat:
            out[f"SaturationAdjustment{ch}"] = int(round(sat[key]))
        if key in lum:
            out[f"LuminanceAdjustment{ch}"] = int(round(lum[key]))
    # Only documented develop-setting keys are emitted: Lightroom rejects a
    # settings table that contains unknown entries, and the plug-in filters
    # defensively as well (lightroom/Kestrel.lrplugin/Apply.lua).
    return out


def local_mask_payload(plan: EditPlan) -> Dict[str, Any]:
    """Describe every local adjustment for the plugin.

    Each entry carries a capability flag so the plugin can route it correctly and
    tell the user when a mask cannot be applied natively.
    """
    masks: List[Dict[str, Any]] = []
    for layer in plan.masks:
        if not layer.enabled:
            continue
        kind = layer.kind
        if kind in ("radial", "linear"):
            capability = "native"
        elif kind in ("eyes", "highlights", "warm_light", "cool_light", "sky", "grass",
                      "court", "crowd", "clothing", "skin", "face", "subject",
                      "background", "object"):
            capability = "plugin"
        else:
            capability = "render"
        masks.append({
            "name": layer.name,
            "kind": kind,
            "capability": capability,
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
    """The document the Lightroom plugin consumes."""
    return {
        "schema": BRAND["engine_schema"],
        "app": BRAND["name"],
        "engine_version": BRAND["version"],
        "image_id": image_id,
        "version_id": version_id,
        "mode": mode,
        "settings": to_develop_settings(plan, analysis),
        "masks": local_mask_payload(plan),
        "explanation": [e.to_dict() for e in plan.explanation],
        "metrics": plan.metrics.to_dict(),
        "genre": plan.genre,
        "intent": plan.intent,
        "strength": plan.strength,
        "warnings": list(plan.warnings) + [w for w in plan.warnings if False],
    }


def capability_report() -> Dict[str, Any]:
    return {
        "global_settings": "native",
        "tone_curve": "native",
        "hsl": "native",
        "detail": "native",
        "lens_corrections": "native",
        "radial_linear_masks": "native",
        "ai_masks": "plugin (Lightroom SDK, undocumented mask API) or render mode",
        "ai_noise_reduction": "render mode (Lightroom's own AI Denoise is not scriptable)",
        "deconvolution": "render mode",
        "object_removal": "not implemented",
        "perspective_correction": "native (LrDevelopController / Upright), not automated",
    }
