"""Evaluation framework.

Measures the properties the brief cares about, on real files:

* exposure accuracy against the genre target,
* colour shift on skin (ΔE) and skin consistency across the frame,
* objective clipping and highlight recovery,
* noise floor before/after, sharpness gain, halo risk,
* naturalness and an aggregate score,
* consistency between images in a set.

Where a human-edited reference exists, Kestrel also reports how close the AI edit
lands to it (ΔE / contrast / saturation distance) — and, importantly, whether it
overshoots the reference (more contrast/clipping than a human would accept).
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .. import imageio
from ..core import utcnow
from ..render.metrics import compute_metrics


def evaluate_image(eng, image_id: str, *, intent: str = "automatic",
                   quality: str = "high", reference_path: Optional[str] = None,
                   profile_id: Optional[str] = None) -> Dict[str, Any]:
    analysis, masks, decoded = eng.analyse(image_id, quality=quality)
    plan, analysis, masks = eng.build_plan(image_id, intent=intent, quality=quality,
                                           profile_id=profile_id, analysis=analysis,
                                           masks=masks)
    out, plan, report = eng.render_pixels(image_id, plan, quality=quality,
                                          max_edge=1600, analysis=analysis, masks=masks)
    mask_dict = {k: masks.get(k) for k in
                 ("subject", "background", "face", "skin", "sky", "grass", "court",
                  "crowd", "clothing") if masks.get(k) is not None}
    metrics = plan.metrics
    result: Dict[str, Any] = {
        "image_id": image_id, "filename": eng.db.get_image(image_id, light=True).filename,
        "genre": analysis.genre, "genre_confidence": analysis.genre_confidence,
        "intent": intent, "quality": quality,
        "metrics": metrics.to_dict(),
        "analysis_summary": {
            "iso_estimate": analysis.quality.iso_estimate,
            "noise": round(analysis.quality.noise_sigma / 100, 4),
            "sharpness": round(analysis.quality.sharpness, 3),
            "faces": len(analysis.faces),
            "subject_confidence": round(analysis.subject_confidence, 3),
            "scene": analysis.scene.event_tags,
            "clipping_before": round(analysis.exposure.clipping_high, 4),
        },
        "before": {
            "contrast": round(analysis.exposure.p95 - analysis.exposure.p05, 4),
            "saturation": round(analysis.colour.saturation_mean, 4),
            "noise": round(analysis.quality.noise_sigma, 3),
            "sharpness": round(analysis.quality.sharpness, 3),
            "skin_delta_e": 0.0,
        },
        "render_report": report.to_dict(),
        "passes": plan.iterations,
        "warnings": plan.warnings,
    }
    if reference_path and Path(reference_path).exists():
        ref = imageio.decode(reference_path, max_edge=1600)
        ref_rgb = ref.rgb
        if ref_rgb.shape[:2] != out.shape[:2]:
            import cv2
            ref_rgb = cv2.resize(ref_rgb, (out.shape[1], out.shape[0]))
        result["reference"] = _compare_to_reference(out, ref_rgb, mask_dict, analysis.genre)
    result["score"] = _score_image(result)
    return result


def _compare_to_reference(edit: np.ndarray, ref: np.ndarray, masks: Dict[str, np.ndarray],
                          genre: str) -> Dict[str, Any]:
    from .. import color
    de = float(np.mean(color.delta_e76(color.rgb_to_lab(edit)[::4, ::4],
                                       color.rgb_to_lab(ref)[::4, ::4])))
    lum_e = float(np.mean(np.abs(color.luma_display(edit) - color.luma_display(ref))))
    cc = float(np.mean(color.rgb_to_lab(edit)[::4, ::4][..., 1:]) )
    ref_cc = float(np.mean(color.rgb_to_lab(ref)[::4, ::4][..., 1:]))
    sat_e = abs(cc - ref_cc)
    skin = masks.get("skin")
    skin_de = None
    if skin is not None and (skin > 0.6).sum() > 200:
        m = skin > 0.6
        skin_de = float(np.mean(color.delta_e76(color.rgb_to_lab(edit)[m],
                                               color.rgb_to_lab(ref)[m])))
    return {"delta_e_mean": round(de, 3), "luminance_mae": round(lum_e, 4),
            "chroma_distance": round(sat_e, 3),
            "skin_delta_e": round(skin_de, 3) if skin_de is not None else None,
            "overshoot": bool(de > 12 or lum_e > 0.11)}


def _score_image(result: Dict[str, Any]) -> float:
    m = result["metrics"]
    score = m["score"]
    # Penalise edits that drift far from a human reference (if supplied).
    ref = result.get("reference")
    if ref:
        score -= min(0.35, ref["delta_e_mean"] / 40.0)
        if ref.get("overshoot"):
            score -= 0.1
    return round(float(max(0.0, min(1.0, score))), 4)


def evaluate_collection(eng, collection_id: str, *, limit: int = 12,
                        intent: str = "automatic", quality: str = "high",
                        references: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    images, total = eng.db.list_images(collection_id=collection_id, limit=limit)
    results = []
    for rec in images:
        try:
            results.append(evaluate_image(eng, rec.id, intent=intent, quality=quality,
                                          reference_path=(references or {}).get(rec.filename)))
        except Exception as exc:
            results.append({"image_id": rec.id, "filename": rec.filename,
                            "error": str(exc)[:300]})
    ok = [r for r in results if "score" in r]
    summary: Dict[str, Any] = {
        "generated": utcnow(), "collection_id": collection_id, "images_evaluated": len(results),
        "images_ok": len(ok),
    }
    if ok:
        def med(key: str) -> float:
            vals = [r["metrics"][key] for r in ok if key in r["metrics"]]
            return round(float(statistics.median(vals)), 4) if vals else 0.0

        summary.update({
            "median_score": round(float(statistics.median([r["score"] for r in ok])), 4),
            "median_exposure_error": med("exposure_error"),
            "median_clipping_high": med("clipping_high"),
            "median_skin_delta_e": med("skin_delta_e"),
            "median_skin_consistency": med("skin_consistency"),
            "median_naturalness": med("naturalness"),
            "median_haloing": med("haloing"),
            "median_noise": med("noise_level"),
            "median_sharpness": med("sharpness"),
        })
        # consistency across the set: spread of skin tone and contrast
        cons = [r["metrics"]["skin_consistency"] for r in ok]
        summary["set_skin_consistency"] = round(float(np.mean(cons)), 4)
        contrast_spread = float(np.std([r["metrics"]["contrast"] for r in ok]))
        summary["contrast_spread"] = round(contrast_spread, 4)
        summary["failures"] = [
            {"filename": r.get("filename"), "reasons": _fail_reasons(r)}
            for r in ok if _fail_reasons(r)
        ]
    summary["results"] = results
    report_dir = eng.settings.storage_dir / "eval"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"eval-{utcnow().replace(':', '').replace('+', '')}.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["report_path"] = str(path)
    eng.db.audit("eval.run", {"collection": collection_id, "images": len(results)})
    return summary


def _fail_reasons(r: Dict[str, Any]) -> List[str]:
    m = r.get("metrics") or {}
    reasons = []
    if m.get("clipping_high", 0) > 0.02:
        reasons.append("highlight clipping above 2%")
    if m.get("skin_delta_e", 0) > 12:
        reasons.append("skin colour shift above ΔE 12")
    if m.get("haloing", 0) > 0.5:
        reasons.append("halo risk")
    if m.get("naturalness", 1) < 0.55:
        reasons.append("naturalness below 0.55")
    if m.get("noise_level", 0) > 6:
        reasons.append("visible noise after reduction")
    return reasons
