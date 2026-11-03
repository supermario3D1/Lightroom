"""Orchestration of the analysis engine."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from .. import color
from ..core import BRAND, clamp
from ..imageio import DecodedImage
from ..models import (AnalysisResult, FaceBox, SceneInfo, SubjectRegion)
from . import scene as scene_mod
from . import subject as subject_mod
from .colour import analyse_colour
from .exposure import analyse_exposure
from .faces import FaceDetector, face_soft_mask
from .quality import analyse_quality


@dataclass
class Masks:
    subject: Optional[np.ndarray] = None
    background: Optional[np.ndarray] = None
    face: Optional[np.ndarray] = None
    skin: Optional[np.ndarray] = None
    sky: Optional[np.ndarray] = None
    grass: Optional[np.ndarray] = None
    court: Optional[np.ndarray] = None
    crowd: Optional[np.ndarray] = None
    clothing: Optional[np.ndarray] = None
    warm_light: Optional[np.ndarray] = None
    cool_light: Optional[np.ndarray] = None
    highlight_detail: Optional[np.ndarray] = None
    clothing_colours: List[List[float]] = field(default_factory=list)
    people: List[Tuple[float, float, float, float, float]] = field(default_factory=list)

    def get(self, kind: str) -> Optional[np.ndarray]:
        return {"subject": self.subject, "background": self.background, "face": self.face,
                "skin": self.skin, "sky": self.sky, "grass": self.grass,
                "court": self.court, "crowd": self.crowd,
                "clothing": self.clothing, "warm_light": self.warm_light,
                "cool_light": self.cool_light,
                "highlight_detail": self.highlight_detail}.get(kind)


class Analyser:
    def __init__(self, model_dir: Optional[Path] = None):
        self.detector = FaceDetector(model_dir)
        self.backends = {
            "faces": self.detector.backend,
            "segmentation": "saliency+grabcut+guided",
            "people": "hog-svm",
        }

    def analyse(self, decoded: DecodedImage, image_id: str = "",
                forced_genre: Optional[str] = None,
                analysis_edge: int = 1600) -> Tuple[AnalysisResult, Masks]:
        rgb = decoded.rgb
        h, w = rgb.shape[:2]
        timings: Dict[str, float] = {}
        notes: List[str] = []

        def tick(name: str, t0: float) -> None:
            timings[name] = round((time.perf_counter() - t0) * 1000.0, 1)

        t0 = time.perf_counter()
        faces = self.detector.detect(rgb)
        tick("faces", t0)
        primary = []

        t0 = time.perf_counter()
        sky_cov, sky_raw = scene_mod.sky_coverage(rgb)
        green_cov, _ = scene_mod.green_coverage(rgb)
        court_cov, _ = scene_mod.court_coverage(rgb)
        arena = scene_mod.arena_cues(rgb)
        white_fabric = scene_mod.white_fabric_coverage(rgb, faces)
        camera_prior = scene_mod.action_camera_prior(decoded.meta)
        sky = subject_mod.sky_mask(rgb)
        grass = subject_mod.grass_mask(rgb, sky) if green_cov > 0.02 else np.zeros((h, w), np.float32)
        court = subject_mod.court_mask(rgb) if court_cov > 0.03 else np.zeros((h, w), np.float32)
        tick("regions", t0)

        scene = SceneInfo()
        scene.grass_coverage = green_cov
        scene.court_coverage = court_cov
        scene.sky_coverage = sky_cov

        # First-pass genre so subject detection can use the right strategy.
        pre_scores = scene_mod.score_genres(
            rgb, faces, scene,
            subject_coverage=0.0, subject_confidence=0.4 + 0.2 * min(len(faces), 2),
            sharpness=0.45, compression=0.0, white_fabric=white_fabric,
            arena=arena, camera_prior=camera_prior)
        pre_genre, _ = scene_mod.choose_genre(pre_scores, forced=forced_genre)

        t0 = time.perf_counter()
        subj, subj_conf, subj_notes, bbox = subject_mod.subject_mask(
            rgb, faces, forced_genre or pre_genre,
            allow_people_detection=(forced_genre or pre_genre) not in ("landscape", "commercial"))
        people = subject_mod.detect_people(rgb) if (faces or subj_conf > 0.5) else []
        tick("subject", t0)
        # A face only counts as "the subject" when it is big enough to matter or
        # when it sits on the detected subject. Crowd faces must not set exposure.
        if faces:
            biggest = max(f.area for f in faces)
            kept = []
            for f in faces:
                x0 = int(np.clip(f.x, 0, 1) * w); x1 = int(np.clip(f.x + f.w, 0, 1) * w)
                y0 = int(np.clip(f.y, 0, 1) * h); y1 = int(np.clip(f.y + f.h, 0, 1) * h)
                inside = float(subj[y0:y1, x0:x1].mean()) if (x1 > x0 and y1 > y0) else 0.0
                if f.area >= max(0.012, biggest * 0.35) or inside > 0.30:
                    kept.append(f)
            if not kept and biggest >= 0.012:
                kept = [max(faces, key=lambda z: z.area)]
            if len(kept) < len(faces):
                notes.append(f"{len(faces) - len(kept)} background face(s) excluded from "
                             "exposure/skin targets")
            primary = kept
        notes.extend(subj_notes)
        if not faces:
            notes.append("no faces detected")
        background = np.clip(1.0 - subj, 0, 1).astype(np.float32)
        crowd = subject_mod.crowd_mask(rgb, subj, sky)
        clothing, clothing_cols = subject_mod.clothing_mask(rgb, subj, faces)
        skin_targets = primary or faces
        skin = face_soft_mask((h, w), skin_targets, feather=0.45, expand=1.25) * color.skin_prior(rgb) \
            if skin_targets else color.skin_prior(rgb) * 0.0
        skin = np.clip(skin, 0, 1).astype(np.float32)
        warm_light, cool_light = _light_pools(rgb, subj, grass, sky)
        highlight_detail = _highlight_mask(rgb, 0.80)

        t0 = time.perf_counter()
        quality = analyse_quality(rgb, decoded.meta, subject_mask=subj)
        if decoded.meta.iso:
            quality.camera["iso"] = decoded.meta.iso
        tick("quality", t0)

        t0 = time.perf_counter()
        scene = scene_mod.analyse_lighting(rgb, scene, faces, subj, background, sky_cov,
                                           green_cov, court_cov, quality.iso_estimate or decoded.meta.iso)
        scene.crowd_coverage = float(crowd.mean()) if crowd is not None else 0.0
        scene.venue_signature = {k: float(v) for k, v in arena.items()}
        tick("lighting", t0)

        t0 = time.perf_counter()
        exposure = analyse_exposure(rgb, subj,
                                    face_soft_mask((h, w), primary, feather=0.30, expand=0.9)
                                    if primary else None,
                                    background, scene)
        tick("exposure", t0)

        t0 = time.perf_counter()
        colour = analyse_colour(rgb, skin if primary else None, grass, sky, quality)
        tick("colour", t0)

        t0 = time.perf_counter()
        final_scores = scene_mod.score_genres(
            rgb, faces, scene, subject_coverage=float(subj.mean()),
            subject_confidence=subj_conf, sharpness=quality.sharpness,
            compression=quality.compression_artifacts, white_fabric=white_fabric,
            arena=arena, camera_prior=camera_prior)
        genre, genre_conf = scene_mod.choose_genre(final_scores, forced=forced_genre)
        scene.genre_scores = final_scores
        tick("genre", t0)

        # Region statistics (used by the decision engine and the UI breakdown)
        regions: Dict[str, SubjectRegion] = {}
        for kind, mask in (("subject", subj), ("background", background), ("sky", sky),
                           ("grass", grass), ("court", court), ("crowd", crowd),
                           ("skin", skin if primary else None)):
            st = subject_mod.region_stats(rgb, mask, kind)
            if st is not None:
                regions[kind] = st
        if primary:
            fm = face_soft_mask((h, w), primary, feather=0.2, expand=0.9)
            st = subject_mod.region_stats(rgb, fm, "face")
            if st is not None:
                regions["face"] = st

        orientation = "landscape"
        if h > w * 1.05:
            orientation = "portrait"
        elif abs(h - w) <= w * 0.05:
            orientation = "square"

        notes.append(f"face backend: {self.backends['faces']}")
        notes.append("subject masks are heuristic (saliency+GrabCut+guided); review before batch apply")
        if decoded.meta.is_raw:
            notes.append("RAW decoded in linear light at 16-bit")
        if quality.iso_estimate >= 3200:
            notes.append(f"high-ISO estimate (~ISO {quality.iso_estimate}): conservative detail lift")

        result = AnalysisResult(
            image_id=image_id, width=decoded.meta.width or w, height=decoded.meta.height or h,
            orientation=orientation, exposure=exposure, colour=colour, quality=quality,
            scene=scene, faces=faces, regions=regions,
            subject_coverage=float(subj.mean()), subject_confidence=subj_conf,
            subject_bbox=bbox, genre=genre, genre_confidence=genre_conf,
            notes=notes, timings_ms=timings,
            analysis_edge=max(h, w),
            providers=dict(self.backends),
        )
        masks = Masks(subject=subj, background=background,
                      face=face_soft_mask((h, w), primary, feather=0.35, expand=1.12) if primary else None,
                      skin=skin, sky=sky, grass=grass, court=court, crowd=crowd,
                      clothing=clothing, clothing_colours=clothing_cols, people=people,
                      warm_light=warm_light, cool_light=cool_light,
                      highlight_detail=highlight_detail)
        timings["total"] = round(sum(timings.values()), 1)
        return result, masks


def _highlight_mask(rgb: np.ndarray, threshold: float = 0.80) -> np.ndarray:
    """Soft mask of bright, low-saturation surfaces (dresses, kit, white paint)."""
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    sat = np.where(mx > 1e-4, (mx - mn) / np.maximum(mx, 1e-4), 0)
    m = color.smoothstep(mx, threshold, 0.99) * (1.0 - color.smoothstep(sat, 0.10, 0.45))
    return np.clip(m, 0, 1).astype(np.float32)


def _light_pools(rgb: np.ndarray, subject: np.ndarray, grass: np.ndarray,
                 sky: np.ndarray) -> tuple:
    """Warm and cool light pools in the background.

    Mixed-lighting correction needs *where* the light is wrong, not just a global
    average.  This isolates locally warm (tungsten/stage) and cool (daylight
    spill, LED) regions outside the subject so each can be nudged toward neutral.
    """
    lab = color.rgb_to_lab(rgb)
    L, a_, b_ = lab[..., 0], lab[..., 1], lab[..., 2]
    chroma = np.sqrt(a_ * a_ + b_ * b_)
    bg = np.clip(1.0 - subject, 0, 1) * (1.0 - np.clip(grass, 0, 1)) * (1.0 - np.clip(sky, 0, 1) * 0.5)
    warm = color.smoothstep(b_, 14.0, 30.0) * (1.0 - color.smoothstep(chroma, 34.0, 62.0))
    cool = color.smoothstep(-b_, 8.0, 22.0) * (1.0 - color.smoothstep(chroma, 30.0, 55.0))
    warm = (warm * bg * color.smoothstep(L, 12.0, 28.0)).astype(np.float32)
    cool = (cool * bg * color.smoothstep(L, 10.0, 24.0)).astype(np.float32)
    return (warm if warm.mean() > 0.004 else np.zeros_like(warm),
            cool if cool.mean() > 0.004 else np.zeros_like(cool))
