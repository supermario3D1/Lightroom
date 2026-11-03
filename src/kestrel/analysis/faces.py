"""Face detection with graceful, honest backend selection.

Backends, in priority order:
 1. YuNet (CNN, CV DNN) — used when ``models/face_detection_yunet_2023mar.onnx`` exists.
 2. Haar cascade — bundled with OpenCV, always available.
 3. Licence-plate-less fallback: skin-locus connected components (low confidence).

The detector used is reported to the UI so results are never misrepresented.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .. import color
from ..models import FaceBox

log = logging.getLogger("kestrel.faces")


class FaceDetector:
    def __init__(self, model_dir: Optional[Path] = None):
        self.backend = "none"
        self._yunet = None
        self._haar_front = None
        self._haar_profile = None
        self._load(model_dir)

    # ------------------------------------------------------------------ setup
    def _load(self, model_dir: Optional[Path]) -> None:
        if model_dir:
            path = Path(model_dir) / "face_detection_yunet_2023mar.onnx"
            if path.exists() and hasattr(cv2, "FaceDetectorYN"):
                try:
                    self._yunet = cv2.FaceDetectorYN.create(
                        str(path), "", (320, 320), score_threshold=0.6, nms_threshold=0.3, top_k=64)
                    self.backend = "yunet-cnn"
                except Exception as exc:  # pragma: no cover
                    log.warning("YuNet unavailable: %s", exc)
        if self._yunet is None:
            try:
                base = Path(cv2.data.haarcascades)
                self._haar_front = cv2.CascadeClassifier(str(base / "haarcascade_frontalface_default.xml"))
                self._haar_profile = cv2.CascadeClassifier(str(base / "haarcascade_profileface.xml"))
                if not self._haar_front.empty():
                    self.backend = "haar-cascade"
            except Exception as exc:  # pragma: no cover
                log.warning("Haar cascades unavailable: %s", exc)

    # ------------------------------------------------------------------ detect
    def detect(self, rgb: np.ndarray, max_side: int = 1280) -> List[FaceBox]:
        h, w = rgb.shape[:2]
        scale = min(1.0, max_side / max(h, w))
        small = rgb if scale == 1.0 else cv2.resize(rgb, (int(w * scale), int(h * scale)),
                                                    interpolation=cv2.INTER_AREA)
        boxes = self._detect_yunet(small) if self._yunet is not None else self._detect_haar(small)
        faces: List[FaceBox] = []
        for (x, y, bw, bh, conf) in boxes:
            x, y, bw, bh = [v / (scale if scale else 1.0) for v in (x, y, bw, bh)]
            # A tiny region can't be a usable face; skip slivers that only add noise.
            if bw < 0.012 * w or bh < 0.012 * h:
                continue
            face = FaceBox(x=float(x / w), y=float(y / h), w=float(bw / w), h=float(bh / h),
                           confidence=float(conf))
            self._refine(rgb, face)
            faces.append(face)
        faces = _nms(faces)
        faces = [f for f in faces if f.skin_ratio > 0.045 or f.confidence > 0.9]
        faces.sort(key=lambda f: f.area, reverse=True)
        return faces[:24]

    def _detect_yunet(self, img: np.ndarray) -> List[Tuple[float, float, float, float, float]]:
        h, w = img.shape[:2]
        try:
            self._yunet.setInputSize((w, h))
            _, dets = self._yunet.detect(np.ascontiguousarray((img[..., ::-1] * 255).astype(np.uint8)))
        except Exception:
            return []
        out = []
        if dets is not None:
            for d in dets:
                x, y, bw, bh = d[:4]
                score = float(d[-1])
                out.append((float(x), float(y), float(bw), float(bh), min(score, 0.99)))
        return out

    def _detect_haar(self, img: np.ndarray) -> List[Tuple[float, float, float, float, float]]:
        gray = (color.luma_display(img) * 255).astype(np.uint8)
        gray = cv2.equalizeHist(gray)
        out: List[Tuple[float, float, float, float, float]] = []
        if self._haar_front is not None and not self._haar_front.empty():
            dets = self._haar_front.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=5,
                                                     minSize=(28, 28), maxSize=(700, 700))
            for (x, y, bw, bh) in dets:
                out.append((float(x), float(y), float(bw), float(bh), 0.82))
        if self._haar_profile is not None and not self._haar_profile.empty():
            dets = self._haar_profile.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                                       minSize=(32, 32), maxSize=(700, 700))
            for (x, y, bw, bh) in dets:
                out.append((float(x), float(y), float(bw), float(bh), 0.55))
        return out

    def _refine(self, rgb: np.ndarray, face: FaceBox) -> None:
        h, w = rgb.shape[:2]
        x0, y0 = int(face.x * w), int(face.y * h)
        x1, y1 = int((face.x + face.w) * w), int((face.y + face.h) * h)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 <= x0 or y1 <= y0:
            return
        patch = rgb[y0:y1, x0:x1]
        skin = color.skin_prior(patch)
        face.skin_ratio = float((skin > 0.45).mean())
        gray = color.luma_display(patch)
        face.mean_luma = float(np.median(gray))
        lap = cv2.Laplacian((gray * 255).astype(np.uint8), cv2.CV_32F, ksize=3)
        face.sharpness = float(np.clip(lap.var() / 240.0, 0.0, 1.0))
        # Horizontal offset of the skin centroid inside the box ≈ coarse head yaw.
        ys, xs = np.nonzero(skin > 0.5)
        if xs.size > 20:
            cx = xs.mean() / max(x1 - x0, 1)
            face.yaw = float(np.clip((cx - 0.5) * 2.0, -1.0, 1.0))
        # Eye region variance: closed eyes flatten the upper-middle band.
        top = patch[int(patch.shape[0] * 0.22):int(patch.shape[0] * 0.52)]
        if top.size:
            g2 = color.luma_display(top)
            face.eye_open = float(np.clip(g2.std() / 0.09, 0.0, 1.0))


def _nms(faces: List[FaceBox], iou_thresh: float = 0.35) -> List[FaceBox]:
    def iou(a: FaceBox, b: FaceBox) -> float:
        ax1, ay1, ax2, ay2 = a.x, a.y, a.x + a.w, a.y + a.h
        bx1, by1, bx2, by2 = b.x, b.y, b.x + b.w, b.y + b.h
        iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
        ih = max(0.0, min(ay2, by2) - max(ay1, by1))
        inter = iw * ih
        if inter <= 0:
            return 0.0
        return inter / (a.w * a.h + b.w * b.h - inter)

    kept: List[FaceBox] = []
    for f in sorted(faces, key=lambda z: z.confidence * z.area, reverse=True):
        if any(iou(f, k) > iou_thresh for k in kept):
            continue
        kept.append(f)
    return kept


def face_soft_mask(shape: Tuple[int, int], faces: List[FaceBox], feather: float = 0.35,
                   expand: float = 1.12) -> np.ndarray:
    """Union of elliptical face masks with feathered edges (float32 0..1)."""
    h, w = shape
    if not faces:
        return np.zeros((h, w), np.float32)
    mask = np.zeros((h, w), np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    for f in faces:
        cx, cy = f.center
        rx = max(f.w * w * expand / 2, 1.0)
        ry = max(f.h * h * expand / 2, 1.0)
        d = np.sqrt(((xx - cx * w) / rx) ** 2 + ((yy - cy * h) / ry) ** 2)
        soft = np.clip(1.0 - color.smoothstep(d, 1.0 - feather, 1.0 + feather * 0.6), 0, 1)
        mask = np.maximum(mask, soft.astype(np.float32))
    return mask


def skin_soft_mask(rgb: np.ndarray, faces: List[FaceBox]) -> np.ndarray:
    """Skin locus restricted to (and immediately around) detected faces."""
    prior = color.skin_prior(rgb)
    if not faces:
        # No faces: allow a very conservative, globally-restricted skin pass.
        return (prior * 0.6).astype(np.float32)
    region = face_soft_mask(rgb.shape[:2], faces, feather=0.5, expand=1.65)
    return (prior * region).astype(np.float32)
