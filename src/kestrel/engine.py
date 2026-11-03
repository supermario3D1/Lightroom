"""The Kestrel engine: import → analyse → decide → render → version → deliver."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import imageio
from .analysis.analyser import Analyser, Masks
from .core import (BRAND, NotFound, ProcessingError, Settings, ValidationError,
                   clamp, engine as _engine_singleton, human_seconds, new_id, utcnow)
from .decision import recipes
from .decision.intents import get_intent, with_strength
from .lightroom.native import apply_payload
from .library.db import Database
from .models import (AnalysisResult, Collection, EditPlan, ImageRecord, JobRecord,
                     ProfileSpec, Version)
from .personalisation.learning import Personalisation
from .profiles import library as profile_library
from .render.core import RenderConfig, Renderer, RenderReport
from .render.masks import MaskStore
from .render.metrics import compute_metrics, refine_plan

log = logging.getLogger("kestrel.engine")


@dataclass
class PreviewResult:
    image: ImageRecord
    version: Version
    original_jpeg: bytes
    edited_jpeg: bytes
    plan: EditPlan
    report: Dict[str, Any]
    quality: str
    cached: bool = False


class KestrelEngine:
    def __init__(self, settings: Settings, start_worker: bool = True):
        self.settings = settings
        settings.ensure_dirs()
        self.db = Database(settings.db_path)
        self.masks = MaskStore(settings.mask_dir)
        self.analyser = Analyser(model_dir=self._model_dir())
        self.learning = Personalisation(self.db, settings)
        self._decode_cache: Dict[str, Tuple[float, imageio.DecodedImage]] = {}
        self._lock = threading.RLock()
        self._seed_profiles()
        self.db.recover_interrupted()
        self.hardware = None
        from .queueing.queue import JobQueue
        self.queue = JobQueue(self, start_worker=start_worker)

    # ------------------------------------------------------------------ setup
    def _model_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "models"

    def _seed_profiles(self) -> None:
        existing = {p.id for p in self.db.list_profiles()}
        for spec in profile_library.BUILTIN_PROFILES:
            if spec.id not in existing:
                self.db.upsert_profile(spec)

    def close(self) -> None:
        try:
            self.queue.stop()
        except Exception:
            pass
        self.db.close()

    # ------------------------------------------------------------------ import
    def import_files(self, paths: List[str], collection_id: Optional[str] = None,
                     lr_uuids: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        created, skipped, failed = [], [], []
        if not paths:
            raise ValidationError("No files were supplied.")
        for raw in paths:
            p = Path(raw).expanduser()
            try:
                if not p.exists() or not p.is_file():
                    failed.append({"path": raw, "error": "file not found"})
                    continue
                if not imageio.is_supported(p):
                    failed.append({"path": raw,
                                   "error": f"{p.suffix or 'file'} is not a supported format"})
                    continue
                existing = self.db.get_image_by_path(str(p.resolve()))
                if existing is not None:
                    skipped.append({"path": str(p), "id": existing.id, "reason": "already imported"})
                    continue
                digest = imageio.sha256_file(p)
                meta_preview = imageio.decode(p, max_edge=64)
                rec = ImageRecord(
                    path=str(p.resolve()), filename=p.name,
                    collection_id=collection_id, width=meta_preview.meta.width,
                    height=meta_preview.meta.height, bytes=p.stat().st_size,
                    sha256=digest, format=p.suffix.lower().lstrip("."),
                    camera=meta_preview.meta.camera, lens=meta_preview.meta.lens,
                    iso=meta_preview.meta.iso,
                    lr_uuid=(lr_uuids or {}).get(str(p.name)),
                )
                rec = self.db.add_image(rec)
                created.append({"id": rec.id, "filename": rec.filename,
                                "width": rec.width, "height": rec.height})
            except Exception as exc:
                log.exception("import failed for %s", raw)
                failed.append({"path": raw, "error": str(exc)})
        self.db.audit("import", {"created": len(created), "skipped": len(skipped),
                                 "failed": len(failed)})
        return {"created": created, "skipped": skipped, "failed": failed,
                "collection_id": collection_id}

    # ------------------------------------------------------------------ decode
    def _decoded(self, rec: ImageRecord, max_edge: int) -> imageio.DecodedImage:
        key = rec.id
        cached = self._decode_cache.get(key)
        if cached and abs(cached[0] - max_edge) < 1 and Path(rec.path).exists():
            return cached[1]
        decoded = imageio.decode(rec.path, max_edge=max_edge)
        with self._lock:
            if len(self._decode_cache) > 3:
                self._decode_cache.pop(next(iter(self._decode_cache)))
            self._decode_cache[key] = (float(max_edge), decoded)
        return decoded

    def _analysis_hash(self, decoded: imageio.DecodedImage, quality_edge: int) -> str:
        h = hashlib.sha256()
        h.update(f"{BRAND['engine_schema']}:{BRAND['version']}:{quality_edge}".encode())
        h.update(np.ascontiguousarray(decoded.rgb[::8, ::8]).tobytes())
        return h.hexdigest()[:16]

    # ------------------------------------------------------------------ analysis
    def analyse(self, image_id: str, force: bool = False,
                quality: Optional[str] = None) -> Tuple[AnalysisResult, Masks, imageio.DecodedImage]:
        rec = self.db.get_image(image_id)
        q = self.settings.quality(quality)
        edge = int(q["analysis_edge"])
        decoded = self._decoded(rec, edge)
        a_hash = self._analysis_hash(decoded, edge)
        if not force and rec.analysis is not None and getattr(rec, "analysis_hash", None) == a_hash:
            masks = self._load_masks(rec, decoded)
            if masks is not None:
                return rec.analysis, masks, decoded
        # stored hash lives in the analysis row; recompute when missing
        if not force and rec.analysis is not None and self.masks.has(self._mask_key(a_hash, decoded)):
            masks = self._load_masks(rec, decoded)
            if masks is not None:
                return rec.analysis, masks, decoded

        t0 = time.perf_counter()
        analysis, masks = self.analyser.analyse(decoded, image_id=image_id,
                                                analysis_edge=edge)
        analysis.preview_hash = a_hash
        key = self._mask_key(a_hash, decoded)
        self.masks.save(key, {k: masks.get(k) for k in
                              ("subject", "background", "face", "skin", "sky", "grass",
                               "court", "crowd", "clothing", "warm_light", "cool_light",
                               "highlight_detail")})
        self.db.save_analysis(image_id, analysis.to_json(), a_hash)
        self.db.update_image(image_id, genre=analysis.genre, width=decoded.meta.width,
                             height=decoded.meta.height)
        self.db.audit("analyse", {"image": image_id, "genre": analysis.genre,
                                  "ms": round((time.perf_counter() - t0) * 1000)})
        return analysis, masks, decoded

    @staticmethod
    def _mask_key(a_hash: str, decoded: imageio.DecodedImage) -> str:
        return MaskStore.key_for(a_hash, (decoded.rgb.shape[0], decoded.rgb.shape[1]))

    def _load_masks(self, rec: ImageRecord, decoded: imageio.DecodedImage) -> Optional[Masks]:
        a_hash = getattr(rec, "analysis_hash", "") or ""
        if not a_hash:
            return None
        loaded = self.masks.load(self._mask_key(a_hash, decoded), decoded.rgb.shape[:2])
        if not loaded:
            return None
        m = Masks()
        for k, v in loaded.items():
            if hasattr(m, k):
                setattr(m, k, v)
        m.clothing_colours = []
        return m

    # ------------------------------------------------------------------ profiles
    def resolve_profile(self, profile_id: Optional[str], genre: str,
                        tags: Optional[List[str]] = None) -> ProfileSpec:
        if profile_id:
            try:
                return self.db.get_profile(profile_id)
            except NotFound:
                pass
        return profile_library.default_profile_for(genre, tags)

    # ------------------------------------------------------------------ planning
    def build_plan(self, image_id: str, *, intent: str = "automatic",
                   strength: float = 1.0, quality: str = "high",
                   profile_id: Optional[str] = None,
                   manual: Optional[Dict[str, Any]] = None,
                   analysis: Optional[AnalysisResult] = None,
                   masks: Optional[Masks] = None,
                   genre_override: Optional[str] = None) -> Tuple[EditPlan, AnalysisResult, Masks]:
        rec = self.db.get_image(image_id)
        if analysis is None or masks is None:
            analysis, masks, _ = self.analyse(image_id, quality=quality)
        if genre_override:
            analysis.genre = genre_override
        profile = self.resolve_profile(profile_id, analysis.genre,
                                       analysis.scene.event_tags)
        recipe = recipes.build_recipe(analysis, profile, intent, strength, quality,
                                      manual=manual)
        plan = EditPlan(
            image_id=image_id, mode="adaptive", genre=analysis.genre, intent=intent,
            strength=strength, quality=quality, global_=recipe.global_,
            masks=recipe.masks, explanation=recipe.explanation, detail=recipe.detail,
            denoise=recipe.denoise, sharpening=recipe.sharpening, crops=recipe.crop,
            warnings=list(recipe.warnings),
            analysis_hash=getattr(analysis, "preview_hash", "") or "",
            iterations=int(self.settings.quality(quality)["passes"]),
        )
        plan.detail["profile_id"] = profile.id
        plan.detail["profile_name"] = profile.name
        plan.detail["targets"] = recipe.targets

        learned = self.learning.apply(plan, analysis.genre, profile)
        if learned.get("applied"):
            plan.detail["personalisation"] = learned

        with_strength(plan, strength)
        if manual:
            _apply_manual(plan, manual)
        return plan, analysis, masks

    # ------------------------------------------------------------------ render
    def _render_config(self, quality: str, max_edge: Optional[int]) -> RenderConfig:
        q = self.settings.quality(quality)
        return RenderConfig(quality=quality, noise_refine=bool(q["noise_refine"]),
                            deconvolution=bool(q["deconvolution"]),
                            max_edge=max_edge)

    def render_pixels(self, image_id: str, plan: EditPlan, *, quality: str = "high",
                      max_edge: Optional[int] = None,
                      analysis: Optional[AnalysisResult] = None,
                      masks: Optional[Masks] = None,
                      refine: bool = True) -> Tuple[np.ndarray, EditPlan, RenderReport]:
        if analysis is None or masks is None:
            analysis, masks, decoded = self.analyse(image_id, quality=quality)
        else:
            rec = self.db.get_image(image_id)
            decoded = self._decoded(rec, int(self.settings.quality(quality)["analysis_edge"]))
        mask_dict = {k: masks.get(k) for k in
                     ("subject", "background", "face", "skin", "sky", "grass", "court",
                      "crowd", "clothing", "warm_light", "cool_light",
                      "highlight_detail") if masks.get(k) is not None}
        renderer = Renderer(self._render_config(quality, max_edge))
        passes = max(1, int(self.settings.quality(quality)["passes"])) if refine else 1
        meta = _meta_dict(analysis)
        best = None
        working = copy.deepcopy(plan)
        for i in range(passes):
            out, report = renderer.render(decoded.rgb, working, mask_dict, meta)
            metrics = compute_metrics(decoded.rgb, out, mask_dict, working.genre,
                                      working.intent, face_mask=mask_dict.get("face"))
            working.metrics = metrics
            if best is None or metrics.score >= best[1].metrics.score - 1e-6:
                best = (out, working, report)
            if metrics.score >= 0.86 or i == passes - 1:
                break
            before = working.to_json()
            working = refine_plan(copy.deepcopy(working), metrics, working.genre,
                                  working.intent, mask_dict)
            if working.to_json() == before:
                break
        assert best is not None
        out, plan_out, report = best
        plan_out.iterations = passes
        return out, plan_out, report

    # ------------------------------------------------------------------ versions
    def _store_thumb(self, data: bytes, name: str) -> str:
        path = self.settings.storage_dir / "thumbs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def create_version(self, image_id: str, plan: EditPlan,
                       report: Optional[RenderReport] = None,
                       original_jpeg: Optional[bytes] = None,
                       edited_jpeg: Optional[bytes] = None,
                       quality: str = "high", mode: str = "adaptive",
                       profile_id: Optional[str] = None,
                       parent_id: Optional[str] = None,
                       render_path: str = "") -> Version:
        rec = self.db.get_image(image_id)
        previous = self.db.latest_version(image_id)
        vid = new_id("ver_")
        v = Version(id=vid, image_id=image_id, parent_id=parent_id or (previous.id if previous else None),
                    mode=mode, intent=plan.intent, strength=plan.strength,
                    quality=quality, profile_id=profile_id or plan.detail.get("profile_id"),
                    plan=plan, report=report.to_dict() if report else {},
                    original_thumb=previous.original_thumb if previous else "",
                    thumb_path="", render_path=render_path)
        if original_jpeg:
            v.original_thumb = self._store_thumb(original_jpeg, f"{image_id}_orig.jpg")
        if edited_jpeg:
            v.thumb_path = self._store_thumb(edited_jpeg, f"{vid}.jpg")
        if not v.thumb_path and plan.preview_hash and previous:
            v.thumb_path = previous.thumb_path
        self.db.save_version(v)
        self.db.update_image(image_id, status="edited", current_version_id=vid,
                             edition_count=(rec.edition_count or 0) + 1,
                             plan_json=plan.to_dict())
        if plan.preview_hash:
            self.db.update_image(image_id, analysis_hash=plan.preview_hash)
        return v

    # ------------------------------------------------------------------ preview
    def preview(self, image_id: str, *, intent: Optional[str] = None,
                strength: float = 1.0, quality: Optional[str] = None,
                profile_id: Optional[str] = None,
                manual: Optional[Dict[str, Any]] = None,
                genre: Optional[str] = None,
                max_edge: int = 1500, save_version: bool = True) -> PreviewResult:
        rec = self.db.get_image(image_id)
        quality = quality or self.settings.default_quality
        q = self.settings.quality(quality)
        edge = int(q["analysis_edge"])
        decoded = self._decoded(rec, edge)
        analysis, masks, _ = self.analyse(image_id, quality=quality)
        intent = intent or self.settings.default_intent
        plan, analysis, masks = self.build_plan(
            image_id, intent=intent, strength=strength, quality=quality,
            profile_id=profile_id, manual=manual, analysis=analysis, masks=masks,
            genre_override=genre)
        out, plan, report = self.render_pixels(
            image_id, plan, quality=quality,
            max_edge=min(int(q["preview_edge"]), max_edge),
            analysis=analysis, masks=masks)

        original = imageio.encode_jpeg(decoded.rgb, quality=90, max_edge=max_edge)
        edited = imageio.encode_jpeg(out, quality=92, max_edge=max_edge)
        version = None
        if save_version:
            version = self.create_version(image_id, plan, report,
                                          original_jpeg=original, edited_jpeg=edited,
                                          quality=quality)
        else:
            version = Version(id=new_id("ver_"), image_id=image_id, plan=plan,
                              report=report.to_dict(),
                              quality=quality, intent=plan.intent,
                              strength=plan.strength)
        return PreviewResult(image=rec, version=version, original_jpeg=original,
                             edited_jpeg=edited, plan=plan, report=report.to_dict(),
                             quality=quality)

    # ------------------------------------------------------------------ export
    def render_edge_for(self, analysis: AnalysisResult, quality: str) -> Tuple[int, str]:
        """Pick the render resolution the memory budget can support."""
        long_edge = max(analysis.width, analysis.height) or 2000
        budget = float(self.settings.memory_budget_gb)
        max_pixels = budget * 1e9 / (3 * 4 * 7)     # ~7 float32 RGB buffers in flight
        cap_edge = int(math.sqrt(max_pixels))
        if quality == "standard":
            target = min(long_edge, 2000)
        elif quality == "high":
            target = min(long_edge, 3600)
        else:
            target = long_edge
        if target > cap_edge:
            return cap_edge, (f"Rendered at {cap_edge}px on the long edge to stay inside "
                              f"the configured {budget:.1f}GB memory budget. Raise the "
                              "budget in Settings for full-resolution output.")
        return target, ""

    def export_version(self, image_id: str, version_id: Optional[str] = None,
                       quality: Optional[str] = None, fmt: str = "jpeg",
                       write: bool = True) -> Tuple[Path, Dict[str, Any]]:
        rec = self.db.get_image(image_id)
        version = self.db.get_version(version_id) if version_id else self.db.latest_version(image_id)
        if version is None or version.plan is None:
            raise NotFound("There is no edit to export yet. Generate a preview first.")
        quality = quality or version.quality or self.settings.default_quality
        analysis, masks, decoded = self.analyse(image_id, quality=quality)
        target_edge, warning = self.render_edge_for(analysis, quality)
        out, plan, report = self.render_pixels(image_id, version.plan, quality=quality,
                                               max_edge=target_edge, analysis=analysis,
                                               masks=masks)
        fmt = (fmt or "jpeg").lower()
        ext = {"jpeg": "jpg", "jpg": "jpg", "tif": "tif", "tiff": "tif", "png": "png"}.get(fmt, "jpg")
        name = f"{Path(rec.filename).stem}_kestrel_{version.id}.{ext}"
        path = self.settings.render_dir / name
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            if ext == "jpg":
                path.write_bytes(imageio.encode_jpeg(out, quality=95, max_edge=target_edge))
            elif ext == "tif":
                # 16-bit output with an embedded sRGB profile: no colour surprises.
                imageio.write_tiff16(out, path)
            else:
                path.write_bytes(imageio.encode_png(out, bit_depth=16))
        report_dict = report.to_dict() if isinstance(report, RenderReport) else dict(report)
        if warning:
            report_dict.setdefault("warnings", []).append(warning)
        self.db.update_image(image_id, render_report_json=report_dict)
        return path, report_dict

    # ------------------------------------------------------------------ delivery
    def delivery_packet(self, image_id: str, version_id: Optional[str] = None,
                        mode: str = "native") -> Dict[str, Any]:
        rec = self.db.get_image(image_id)
        version = self.db.get_version(version_id) if version_id else self.db.latest_version(image_id)
        if version is None or version.plan is None:
            raise NotFound("Generate an edit before sending it to Lightroom.")
        payload = apply_payload(version.plan, rec.analysis, image_id, version.id, mode)
        payload["filename"] = rec.filename
        payload["lr_uuid"] = rec.lr_uuid
        payload["original_path"] = rec.path
        return payload

    def mark_applied(self, version_id: str) -> None:
        self.db.mark_applied(version_id)
        self.db.audit("apply.lightroom", {"version": version_id})

    # ------------------------------------------------------------------ feedback
    def feedback(self, image_id: str, kind: str, *, version_id: Optional[str] = None,
                 rating: int = 0, manual_delta: Optional[Dict[str, float]] = None,
                 note: str = "") -> Dict[str, Any]:
        if kind not in ("accept", "reject", "rating", "manual_adjust", "prefer"):
            raise ValidationError(f"Unknown feedback kind '{kind}'.")
        rec = self.db.get_image(image_id, light=True)
        version = self.db.get_version(version_id) if version_id else self.db.latest_version(image_id)
        if kind == "accept":
            self.db.update_image(image_id, accepted=True)
        elif kind == "reject":
            self.db.update_image(image_id, accepted=False)
        delta = dict(manual_delta or {})
        if version and version.plan and kind == "manual_adjust":
            delta = dict(delta)
            delta.setdefault("baseline_contrast", version.plan.global_.contrast)
        event = self.db.add_feedback(image_id, version.id if version else None, kind,
                                     rating=rating, delta=delta, note=note)
        learned = self.learning.record(kind, rec.genre or "general", rating=rating,
                                       manual_delta=delta,
                                       plan=version.plan if version else None)
        return {"feedback": event, "learning": learned.to_dict() if learned else None,
                "learning_enabled": self.learning.enabled}


# ---------------------------------------------------------------------------- helpers
def _meta_dict(analysis: AnalysisResult) -> Dict[str, Any]:
    return {
        "lens": analysis.quality.lens,
        "camera": analysis.quality.camera,
        "is_raw": analysis.quality.is_raw,
        "bit_depth": analysis.quality.bit_depth,
        "width": analysis.width,
        "height": analysis.height,
    }


MANUAL_KEYS = (
    "exposure", "contrast", "highlights", "shadows", "whites", "blacks", "texture",
    "clarity", "dehaze", "vibrance", "saturation", "temperature", "tint", "sharpness",
    "luminance_nr", "color_nr", "post_crop_vignette", "grain",
)


def _apply_manual(plan: EditPlan, manual: Dict[str, Any]) -> None:
    """Apply the photographer's manual overrides on top of the AI plan."""
    applied = []
    for key in MANUAL_KEYS:
        if key not in manual or manual[key] is None:
            continue
        try:
            val = float(manual[key])
        except (TypeError, ValueError):
            continue
        if key in ("temperature",):
            val = float(clamp(val, 2000, 12000))
        elif key == "tint":
            val = float(clamp(val, -150, 150))
        elif key == "exposure":
            val = float(clamp(val, -5, 5))
        else:
            val = float(clamp(val, -100, 100))
        setattr(plan.global_, key, val)
        applied.append(key)
    # mask overrides: {"subject": {"exposure": 0.2}, ...}
    for kind, values in (manual.get("masks") or {}).items():
        for layer in plan.masks:
            if layer.kind != kind:
                continue
            for k, v in (values or {}).items():
                if k in MANUAL_KEYS:
                    try:
                        setattr(layer.adjustments, k, float(v))
                    except (TypeError, ValueError):
                        continue
            layer.provenance = "manual"
            applied.append(f"mask:{kind}")
    plan.detail["manual_overrides"] = applied
    if applied and plan.explanation:
        plan.explanation.append(plan.explanation[0].__class__(
            area="Manual overrides",
            action="Applied your manual corrections",
            reason="You adjusted these controls directly; Kestrel kept your values "
                   "instead of its own.",
            amount=", ".join(applied), confidence=1.0))
