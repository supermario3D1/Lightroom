"""Kestrel local HTTP API + bundled web application.

Runs on 127.0.0.1 by default for the desktop/plugin workflow; bind ``0.0.0.0``
explicitly (``--host 0.0.0.0``) when you want the UI reachable from another
device on your own network.
"""
from __future__ import annotations

import io
import json
import logging
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import imageio
from ..core import (BRAND, KestrelError, NotFound, ProcessingError, Settings,
                    ValidationError, clamp, engine as engine_singleton, human_seconds,
                    new_id, utcnow)
from ..license import verify_licence as licence_verify
from ..models import EditPlan, ProfileSpec
from ..profiles import library as profile_library
from ..queueing.batch import build_groups, group_map
from ..render.core import RenderConfig  # noqa: F401
from ..core import QUALITY_PRESETS
from ..lightroom.native import capability_report

log = logging.getLogger("kestrel.api")

# The UI ships inside the package (kestrel/api/static) for wheel installs, and
# lives in ./web during development. Both are supported; the packaged copy wins
# so an installed build can never accidentally serve a stale development file.
_HERE = Path(__file__).resolve().parent
_PACKAGED_WEB = _HERE / "static"
_REPO_ROOT = _HERE.parents[2]
WEB_DIR = _PACKAGED_WEB if (_PACKAGED_WEB / "index.html").exists() else _REPO_ROOT / "web"
BRAND_DIR = ((_PACKAGED_WEB / "brand") if (_PACKAGED_WEB / "brand").exists()
             else (_REPO_ROOT / "assets" / "brand"))


# ---------------------------------------------------------------------------- schemas
class ImportRequest(BaseModel):
    paths: List[str] = Field(default_factory=list)
    collection_id: Optional[str] = None
    lr_uuids: Dict[str, str] = Field(default_factory=dict)


class CollectionRequest(BaseModel):
    name: str
    genre_hint: str = ""
    notes: str = ""
    profile_id: Optional[str] = None


class PreviewRequest(BaseModel):
    intent: Optional[str] = None
    strength: float = 1.0
    quality: Optional[str] = None
    profile_id: Optional[str] = None
    manual: Optional[Dict[str, Any]] = None
    genre: Optional[str] = None
    save_version: bool = True
    max_edge: int = 1500


class FeedbackRequest(BaseModel):
    kind: str
    version_id: Optional[str] = None
    rating: int = 0
    manual_delta: Optional[Dict[str, float]] = None
    note: str = ""


class ApplyRequest(BaseModel):
    version_id: Optional[str] = None
    mode: str = "native"


class ExportRequest(BaseModel):
    version_id: Optional[str] = None
    quality: Optional[str] = None
    format: str = "jpeg"


class JobRequest(BaseModel):
    kind: str = "edit"
    image_ids: List[str] = Field(default_factory=list)
    collection_id: Optional[str] = None
    priority: int = 5
    options: Dict[str, Any] = Field(default_factory=dict)


class ProfilePayload(BaseModel):
    profile: Dict[str, Any]


class SettingsPayload(BaseModel):
    values: Dict[str, Any]


class LicenceRequest(BaseModel):
    key: str
    email: str = ""


# ---------------------------------------------------------------------------- app
def create_app(engine=None) -> FastAPI:
    eng = engine or engine_singleton()
    settings = eng.settings

    app = FastAPI(title=f"{BRAND['name']} API", version=BRAND["version"],
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )

    @app.exception_handler(KestrelError)
    async def kestrel_error(request: Request, exc: KestrelError):
        return JSONResponse(exc.to_dict(), status_code=exc.http_status)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"error": "internal_error", "message": str(exc)[:400],
                             "hint": "Check the Kestrel log for the full traceback."},
                            status_code=500)

    # ---------------------------------------------------------------- health
    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        hw = eng.hardware
        if hw is None:
            from ..core import HardwareProfile
            hw = HardwareProfile.detect()
            eng.hardware = hw
        return {
            "app": BRAND["name"], "version": BRAND["version"], "schema": BRAND["engine_schema"],
            "status": "ok", "time": utcnow(),
            "python": sys.version.split()[0], "platform": platform.platform(),
            "hardware": hw.to_dict(),
            "engine": {
                "analyser_backends": eng.analyser.backends,
                "renderer": "kestrel multi-stage linear/display pipeline",
                "analysis_edge_default": settings.quality()["analysis_edge"],
                "max_workers": settings.max_workers,
                "memory_budget_gb": settings.memory_budget_gb,
            },
            "capabilities": capability_report(),
            "quality_presets": QUALITY_PRESETS,
            "limits": {
                "max_batch_recommended": 5000,
                "local_only": True,
                "cloud_processing": False,
                "deep_learning_models": _model_status(eng),
            },
        }

    # ---------------------------------------------------------------- collections
    @app.get("/api/collections")
    def list_collections() -> Dict[str, Any]:
        return {"collections": eng.db.list_collections()}

    @app.post("/api/collections")
    def create_collection(req: CollectionRequest) -> Dict[str, Any]:
        col = eng.db.create_collection(req.name, req.genre_hint, req.notes, req.profile_id)
        return {"collection": col.to_dict()}

    @app.patch("/api/collections/{cid}")
    def update_collection(cid: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        eng.db.update_collection(cid, **payload)
        return {"collection": eng.db.get_collection(cid).to_dict()}

    @app.delete("/api/collections/{cid}")
    def delete_collection(cid: str, delete_images: bool = False) -> Dict[str, Any]:
        eng.db.delete_collection(cid, delete_images=delete_images)
        return {"deleted": cid}

    @app.get("/api/collections/{cid}/consistency")
    def consistency(cid: str, refresh: bool = False) -> Dict[str, Any]:
        cached = eng.db.get_pref(f"consistency.{cid}")
        if cached and not refresh:
            return {"groups": cached.get("groups", []), "built": cached.get("built"), "cached": True}
        images, _ = eng.db.list_images(collection_id=cid, limit=20000)
        analyses = {}
        for rec in images:
            a = rec.analysis or (eng.db.get_image(rec.id).analysis)
            if a is not None:
                analyses[rec.id] = a
        if len(analyses) < 2:
            raise ValidationError("Analyse at least two photographs to build a consistency model.")
        groups = build_groups(analyses)
        eng.db.set_pref(f"consistency.{cid}",
                        {"built": utcnow(), "groups": [g.to_dict() for g in groups]})
        return {"groups": [g.to_dict() for g in groups], "built": utcnow(), "cached": False}

    # ---------------------------------------------------------------- import
    @app.post("/api/import")
    def import_paths(req: ImportRequest) -> Dict[str, Any]:
        return eng.import_files(req.paths, req.collection_id, req.lr_uuids)

    @app.post("/api/import/upload")
    async def import_upload(files: List[UploadFile] = File(...),
                            collection_id: Optional[str] = None) -> Dict[str, Any]:
        saved = []
        for f in files:
            name = Path(f.filename or "upload.jpg").name
            if Path(name).suffix.lower() not in imageio.SUPPORTED_EXTENSIONS:
                continue
            dest = eng.settings.import_dir / f"{int(time.time())}_{name}"
            with open(dest, "wb") as fh:
                shutil.copyfileobj(f.file, fh)
            saved.append(str(dest))
        return eng.import_files(saved, collection_id)

    # ---------------------------------------------------------------- images
    @app.get("/api/images")
    def list_images(collection_id: Optional[str] = None, status: Optional[str] = None,
                    search: str = "", limit: int = 200, offset: int = 0) -> Dict[str, Any]:
        images, total = eng.db.list_images(collection_id=collection_id, status=status,
                                           search=search, limit=min(limit, 1000),
                                           offset=offset)
        return {"images": [_image_summary(i) for i in images], "total": total}

    @app.get("/api/images/{iid}")
    def get_image(iid: str) -> Dict[str, Any]:
        rec = eng.db.get_image(iid)
        versions = eng.db.list_versions(iid, limit=20)
        return {"image": _image_detail(rec), "versions": [_version_summary(v) for v in versions],
                "queue": eng.queue.status()}

    @app.post("/api/images/{iid}/analyse")
    def analyse_image(iid: str, force: bool = False, quality: Optional[str] = None) -> Dict[str, Any]:
        analysis, masks, decoded = eng.analyse(iid, force=force, quality=quality)
        rec = eng.db.get_image(iid, light=True)
        return {"analysis": analysis.to_dict(), "image": _image_summary(rec),
                "masks": _mask_summary(masks), "providers": analysis.providers}

    @app.post("/api/images/{iid}/preview")
    def preview(iid: str, req: PreviewRequest) -> Dict[str, Any]:
        result = eng.preview(iid, intent=req.intent, strength=req.strength,
                             quality=req.quality, profile_id=req.profile_id,
                             manual=req.manual, genre=req.genre,
                             max_edge=max(400, min(req.max_edge, 4000)),
                             save_version=req.save_version)
        return _preview_payload(eng, result)

    @app.get("/api/images/{iid}/mask/{kind}")
    def mask_overlay(iid: str, kind: str, max_edge: int = 1400, colour: str = "gold",
                     opacity: float = 0.55) -> Response:
        """Render one analysis mask over the original, so masks can be reviewed.

        This exists because mask confidence alone is not enough to judge whether a
        season-long batch is safe: the photographer has to be able to *see* what
        the engine selected before trusting it.
        """
        import cv2
        import numpy as np

        rec = eng.db.get_image(iid, light=True)
        analysis, masks, decoded = eng.analyse(iid)
        if kind not in {"subject", "background", "face", "skin", "sky", "grass", "court",
                        "crowd", "clothing", "warm_light", "cool_light", "highlight_detail"}:
            raise ValidationError(f"'{kind}' is not a mask this engine produces.",
                                  hint="Use one of the masks listed in the analysis panel.")
        mask = masks.get(kind) if hasattr(masks, "get") else None
        if mask is None or float(np.max(mask)) <= 0.01:
            raise NotFound(f"The {kind} mask is empty for this photograph.")

        edge = max(320, min(int(max_edge), 2400))
        rgb = decoded.rgb
        h, w = rgb.shape[:2]
        scale = min(1.0, edge / max(h, w))
        if scale < 1.0:
            size = (int(w * scale), int(h * scale))
            rgb = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
            mask = cv2.resize(mask, size, interpolation=cv2.INTER_LINEAR)
        base = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)

        palette = {"gold": (224, 164, 88), "teal": (116, 195, 172),
                   "red": (226, 112, 95), "blue": (109, 168, 214)}
        tint = np.array(palette.get(colour, palette["gold"]), dtype=np.float32)[::-1]
        weight = np.clip(float(opacity), 0.05, 0.95) * np.clip(mask, 0, 1)[:, :, None]
        tinted = base.astype(np.float32) * (1.0 - weight) + tint * weight
        # outline the mask edge so the boundary is legible over busy backgrounds
        edges = cv2.Canny((np.clip(mask, 0, 1) * 255).astype(np.uint8), 40, 120)
        tinted[edges > 0] = np.array([248, 250, 252], dtype=np.float32)
        out = np.clip(tinted, 0, 255).astype(np.uint8)
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            raise ProcessingError("The mask overlay could not be encoded.")
        return Response(buf.tobytes(), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=300"})

    @app.get("/api/images/{iid}/original")
    def original_image(iid: str, max_edge: int = 1600) -> Response:
        rec = eng.db.get_image(iid, light=True)
        decoded = imageio.decode(rec.path, max_edge=max(400, min(max_edge, 4096)))
        return Response(imageio.encode_jpeg(decoded.rgb, quality=90, max_edge=max_edge),
                        media_type="image/jpeg", headers={"Cache-Control": "max-age=600"})

    @app.delete("/api/images/{iid}")
    def delete_image(iid: str) -> Dict[str, Any]:
        eng.db.delete_image(iid)
        return {"deleted": iid}

    # ---------------------------------------------------------------- feedback
    @app.post("/api/images/{iid}/feedback")
    def feedback(iid: str, req: FeedbackRequest) -> Dict[str, Any]:
        return eng.feedback(iid, req.kind, version_id=req.version_id, rating=req.rating,
                            manual_delta=req.manual_delta, note=req.note)

    @app.get("/api/images/{iid}/versions")
    def versions(iid: str) -> Dict[str, Any]:
        return {"versions": [_version_summary(v) for v in eng.db.list_versions(iid)]}

    # ---------------------------------------------------------------- delivery
    @app.post("/api/images/{iid}/apply")
    def apply_to_lightroom(iid: str, req: ApplyRequest) -> Dict[str, Any]:
        payload = eng.delivery_packet(iid, req.version_id, mode=req.mode)
        path = eng.settings.storage_dir / "delivery" / f"{iid}_{payload['version_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        payload["packet_path"] = str(path)
        return {"payload": payload}

    @app.post("/api/images/{iid}/export")
    def export_image(iid: str, req: ExportRequest) -> Dict[str, Any]:
        path, report = eng.export_version(iid, req.version_id, quality=req.quality,
                                          fmt=req.format)
        rec = eng.db.get_image(iid, light=True)
        return {"path": str(path), "download": f"/api/files/{path.name}",
                "filename": path.name, "report": report,
                "original": rec.path, "lr_uuid": rec.lr_uuid}

    @app.get("/api/files/{name}")
    def download(name: str) -> FileResponse:
        safe = Path(name).name
        for base in (eng.settings.render_dir, eng.settings.storage_dir):
            p = base / safe
            if p.exists() and p.is_file():
                return FileResponse(p, filename=safe)
        raise NotFound("That file is no longer available.")

    @app.get("/api/versions/{vid}")
    def get_version(vid: str) -> Dict[str, Any]:
        v = eng.db.get_version(vid)
        return {"version": v.to_dict()}

    @app.get("/api/versions/{vid}/thumb")
    def version_thumb(vid: str) -> FileResponse:
        v = eng.db.get_version(vid)
        p = Path(v.thumb_path)
        if not p.exists():
            raise NotFound("That preview image is no longer on disk.")
        return FileResponse(p, media_type="image/jpeg")

    @app.get("/api/versions/{vid}/original-thumb")
    def version_original_thumb(vid: str) -> FileResponse:
        v = eng.db.get_version(vid)
        p = Path(v.original_thumb)
        if not p.exists():
            raise NotFound("That preview image is no longer on disk.")
        return FileResponse(p, media_type="image/jpeg")

    @app.get("/api/versions/{vid}/render")
    def version_render(vid: str) -> FileResponse:
        v = eng.db.get_version(vid)
        p = Path(v.render_path)
        if not v.render_path or not p.exists():
            raise NotFound("No rendered file exists for this version yet.")
        return FileResponse(p, filename=p.name)

    # ---------------------------------------------------------------- profiles
    @app.get("/api/profiles")
    def list_profiles(genre: Optional[str] = None) -> Dict[str, Any]:
        profiles = eng.db.list_profiles()
        if genre:
            profiles = [p for p in profiles if p.genre == genre]
        return {"profiles": [p.to_dict() for p in profiles],
                "genres": sorted({p.genre for p in eng.db.list_profiles()})}

    @app.post("/api/profiles")
    def create_profile(payload: ProfilePayload) -> Dict[str, Any]:
        data = dict(payload.profile)
        data.pop("id", None)
        spec = ProfileSpec(**{f: v for f, v in data.items()
                              if f in ProfileSpec.__dataclass_fields__})
        spec.id = new_id("prof_")
        spec.builtin = False
        spec.created = spec.updated = utcnow()
        if not spec.name:
            spec.name = "New profile"
        profile_library.validate_profile(spec)
        eng.db.upsert_profile(spec)
        eng.db.audit("profile.create", {"id": spec.id, "name": spec.name})
        return {"profile": spec.to_dict()}

    @app.patch("/api/profiles/{pid}")
    def update_profile(pid: str, payload: ProfilePayload) -> Dict[str, Any]:
        current = eng.db.get_profile(pid)
        if current.builtin:
            raise ValidationError("Built-in profiles are read-only — duplicate it to customise.")
        data = {f: v for f, v in payload.profile.items()
                if f in ProfileSpec.__dataclass_fields__}
        merged = current.to_dict()
        merged.update(data)
        merged.pop("id", None)
        spec = ProfileSpec(**{f: v for f, v in merged.items()
                              if f in ProfileSpec.__dataclass_fields__})
        spec.id = pid
        spec.builtin = False
        profile_library.validate_profile(spec)
        eng.db.upsert_profile(spec)
        return {"profile": spec.to_dict()}

    @app.post("/api/profiles/{pid}/duplicate")
    def duplicate_profile(pid: str) -> Dict[str, Any]:
        spec = eng.db.get_profile(pid)
        clone = profile_library.clone_profile(spec)
        eng.db.upsert_profile(clone)
        return {"profile": clone.to_dict()}

    @app.delete("/api/profiles/{pid}")
    def delete_profile(pid: str) -> Dict[str, Any]:
        eng.db.delete_profile(pid)
        return {"deleted": pid}

    @app.get("/api/profiles/{pid}/export")
    def export_profile(pid: str) -> Response:
        spec = eng.db.get_profile(pid)
        body = profile_library.export_profile(spec)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{_slug(spec.name)}.kestrelprofile.json"'})

    @app.post("/api/profiles/import")
    def import_profile(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        spec = profile_library.import_profile(json.dumps(payload.get("document", payload)))
        eng.db.upsert_profile(spec)
        return {"profile": spec.to_dict()}

    # ---------------------------------------------------------------- jobs
    @app.get("/api/jobs")
    def list_jobs() -> Dict[str, Any]:
        return eng.queue.status()

    @app.post("/api/jobs")
    def create_job(req: JobRequest) -> Dict[str, Any]:
        if req.collection_id and not req.image_ids:
            job = eng.queue.submit_collection(req.collection_id, kind=req.kind,
                                              priority=req.priority, options=req.options)
        else:
            job = eng.queue.submit(req.kind, req.image_ids, collection_id=req.collection_id,
                                   priority=req.priority, options=req.options)
        return {"job": job.to_dict()}

    @app.get("/api/jobs/{jid}")
    def get_job(jid: str) -> Dict[str, Any]:
        return {"job": eng.db.get_job(jid).to_dict(), "items": eng.db.job_items(jid)}

    @app.post("/api/jobs/{jid}/pause")
    def pause_job(jid: str) -> Dict[str, Any]:
        return {"job": eng.queue.pause(jid)}

    @app.post("/api/jobs/{jid}/resume")
    def resume_job(jid: str) -> Dict[str, Any]:
        return {"job": eng.queue.resume(jid)}

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: str) -> Dict[str, Any]:
        return {"job": eng.queue.cancel(jid)}

    @app.post("/api/jobs/{jid}/retry")
    def retry_job(jid: str) -> Dict[str, Any]:
        return eng.queue.retry_failed(jid)

    # ---------------------------------------------------------------- stats/settings
    @app.get("/api/stats")
    def stats() -> Dict[str, Any]:
        s = eng.db.stats()
        s["recent_jobs"] = eng.db.list_jobs(limit=5)
        s["queue"] = eng.queue.status()
        s["learning"] = eng.learning.summary()
        return s

    @app.get("/api/settings")
    def get_settings() -> Dict[str, Any]:
        d = settings.__dict__.copy()
        d["home"] = str(settings.home)
        d["quality_presets"] = QUALITY_PRESETS
        try:
            from ..core import HardwareProfile
            d["hardware"] = (eng.hardware or HardwareProfile.detect()).to_dict()
        except Exception:
            pass
        return {"settings": d}

    @app.put("/api/settings")
    def update_settings(req: SettingsPayload) -> Dict[str, Any]:
        mutable = {"default_quality", "default_intent", "max_workers", "memory_budget_gb",
                   "preserve_input_profile", "write_sidecars", "keep_intermediate_tiffs",
                   "personalised_learning", "retention_days", "log_level", "user_name",
                   "onboarding_complete", "allow_cloud", "telemetry", "update_channel"}
        changed = {}
        for k, v in req.values.items():
            if k not in mutable:
                continue
            if k == "memory_budget_gb":
                v = float(clamp(float(v), 0.6, 64.0))
            if k == "max_workers":
                v = int(clamp(int(v), 1, 4))
            if k == "default_quality" and v not in QUALITY_PRESETS:
                continue
            setattr(settings, k, v)
            changed[k] = v
        settings.save()
        eng.db.audit("settings.update", changed)
        return {"settings": {k: getattr(settings, k) for k in changed}}

    @app.get("/api/personalisation")
    def personalisation() -> Dict[str, Any]:
        return eng.learning.summary()

    @app.post("/api/personalisation/reset")
    def personalisation_reset() -> Dict[str, Any]:
        eng.learning.reset()
        return eng.learning.summary()

    @app.post("/api/personalisation/consent")
    def personalisation_consent(enabled: bool = Body(..., embed=True)) -> Dict[str, Any]:
        settings.personalised_learning = bool(enabled)
        settings.save()
        eng.db.audit("personalisation.consent", {"enabled": bool(enabled)})
        return eng.learning.summary()

    @app.get("/api/licence")
    def licence() -> Dict[str, Any]:
        return licence_verify(settings)

    @app.post("/api/licence")
    def activate_licence(req: LicenceRequest) -> Dict[str, Any]:
        from ..license import activate
        return activate(settings, req.key, req.email)

    @app.get("/api/logs")
    def logs(tail: int = 200) -> Dict[str, Any]:
        p = settings.log_path
        if not p.exists():
            return {"lines": []}
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-min(tail, 2000):]
        return {"lines": lines}

    @app.get("/api/capabilities")
    def capabilities() -> Dict[str, Any]:
        return capability_report()

    # ---------------------------------------------------------------- demo library
    @app.post("/api/demo/load")
    def load_demo() -> Dict[str, Any]:
        from ..demo import load_demo_library
        return load_demo_library(eng)

    # ---------------------------------------------------------------- static UI
    if WEB_DIR.exists():
        app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")
    if BRAND_DIR.exists():
        app.mount("/brand", StaticFiles(directory=str(BRAND_DIR)), name="brand")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        index_path = WEB_DIR / "index.html"
        if not index_path.exists():
            return HTMLResponse("<h1>Kestrel UI not found</h1>", status_code=500)
        return HTMLResponse(index_path.read_text(encoding="utf-8"))

    @app.get("/favicon.ico")
    def favicon():
        p = WEB_DIR / "favicon.svg"
        if p.exists():
            return FileResponse(p, media_type="image/svg+xml")
        return Response(status_code=204)

    @app.get("/favicon.svg")
    def favicon_svg() -> FileResponse:
        return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")

    def _script(name: str) -> FileResponse:
        return FileResponse(WEB_DIR / name, media_type="application/javascript")

    @app.get("/app.js")
    def app_js() -> FileResponse:
        return _script("app.js")

    @app.get("/views.js")
    def views_js() -> FileResponse:
        return _script("views.js")

    @app.get("/editor.js")
    def editor_js() -> FileResponse:
        return _script("editor.js")

    @app.get("/styles.css")
    def styles() -> FileResponse:
        return FileResponse(WEB_DIR / "styles.css", media_type="text/css")

    return app


# ---------------------------------------------------------------------------- helpers
def _slug(text: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in text).strip("-")


def _model_status(eng) -> Dict[str, Any]:
    model_dir = _REPO_ROOT / "models"
    files = sorted(p.name for p in model_dir.glob("*.onnx")) if model_dir.exists() else []
    try:
        import onnxruntime  # noqa: F401
        runtime = "onnxruntime available"
    except Exception:
        runtime = "onnxruntime not installed"
    return {"directory": str(model_dir), "installed": files, "runtime": runtime,
            "faces": eng.analyser.backends.get("faces"),
            "note": "Kestrel runs fully with classical CV. If a compatible"
                    " segmentation/face ONNX model is placed in models/, it is used"
                    " automatically and reported in the health endpoint."}


def _image_summary(rec) -> Dict[str, Any]:
    return {
        "id": rec.id, "filename": rec.filename, "path": rec.path,
        "collection_id": rec.collection_id, "width": rec.width, "height": rec.height,
        "status": rec.status, "genre": rec.genre, "camera": rec.camera, "lens": rec.lens,
        "iso": rec.iso, "rating": rec.rating, "accepted": rec.accepted,
        "edition_count": rec.edition_count, "current_version_id": rec.current_version_id,
        "error": rec.error, "imported": rec.imported,
        "format": rec.format, "bytes": rec.bytes,
        "thumb": f"/api/images/{rec.id}/original?max_edge=480" if rec.status != "new" else None,
    }


def _image_detail(rec) -> Dict[str, Any]:
    d = _image_summary(rec)
    d["analysis"] = rec.analysis.to_dict() if rec.analysis else None
    d["plan"] = rec.plan.to_dict() if rec.plan else None
    d["render_report"] = rec.render_report
    return d


def _version_summary(v) -> Dict[str, Any]:
    return {
        "id": v.id, "image_id": v.image_id, "created": v.created, "mode": v.mode,
        "intent": v.intent, "strength": v.strength, "quality": v.quality,
        "profile_id": v.profile_id, "applied_to_lightroom": v.applied_to_lightroom,
        "feedback": v.feedback, "note": v.note,
        "metrics": v.plan.metrics.to_dict() if v.plan else {},
        "thumb": f"/api/versions/{v.id}/thumb" if v.thumb_path else None,
        "original_thumb": f"/api/versions/{v.id}/original-thumb" if v.original_thumb else None,
        "explanation": [e.to_dict() for e in v.plan.explanation] if v.plan else [],
        "settings": v.plan.settings_summary() if v.plan else [],
        "masks": [{"name": m.name, "kind": m.kind, "enabled": m.enabled,
                   "opacity": m.opacity, "confidence": m.confidence,
                   "notes": m.notes, "provenance": m.provenance}
                  for m in (v.plan.masks if v.plan else [])],
        "report": v.report,
        "warnings": v.plan.warnings if v.plan else [],
    }


def _mask_summary(masks) -> Dict[str, Any]:
    out = {}
    for k in ("subject", "background", "face", "skin", "sky", "grass", "court",
              "crowd", "clothing", "warm_light", "cool_light", "highlight_detail"):
        m = masks.get(k)
        if m is None:
            continue
        out[k] = {"coverage": round(float(m.mean()), 4), "max": round(float(m.max()), 3)}
    return out


def _preview_payload(eng, result) -> Dict[str, Any]:
    v = result.version
    rec = result.image
    plan = result.plan
    analysis = None
    try:
        analysis = eng.db.get_image(rec.id).analysis
    except Exception:
        pass
    return {
        "image": _image_summary(rec),
        "version": _version_summary(v),
        "plan": plan.to_dict(),
        "analysis": analysis.to_dict() if analysis else None,
        "original_url": f"/api/versions/{v.id}/original-thumb",
        "edited_url": f"/api/versions/{v.id}/thumb",
        "report": result.report,
        "quality": result.quality,
        "settings": plan.settings_summary(),
        "explanation": [e.to_dict() for e in plan.explanation],
        "masks": [{"name": m.name, "kind": m.kind, "confidence": m.confidence,
                   "opacity": round(m.opacity, 2), "notes": m.notes,
                   "kind_capability": m.provenance, "enabled": m.enabled}
                  for m in plan.masks],
        "warnings": plan.warnings + list(result.report.get("warnings", [])),
        "metrics": plan.metrics.to_dict(),
        "crop": plan.crops,
        "denoise": plan.denoise,
        "sharpening": plan.sharpening,
        "profile": plan.detail.get("profile_name"),
        "personalisation": plan.detail.get("personalisation"),
    }
