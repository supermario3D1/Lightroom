"""Subject, background and semantic region masking.

Masks are *soft*, edge-aware and individually justifiable: each one is built from
several independent cues (saliency, faces, people detection, colour, geometry)
and then refined jointly with the image using GrabCut and a guided filter.

Honest limitation: without a licensed deep segmentation model (SAM/DeepLab) the
engine cannot guarantee pixel-perfect subject isolation on every scene.  It
therefore reports confidence, and the plugin exposes every mask for review and
manual override.  If a segmentation ONNX model is installed in ``models/`` the
engine automatically uses it (see analysis/segmentation_backends.py).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from .. import color
from ..core import clamp
from ..models import FaceBox, SubjectRegion
from . import scene as scene_mod
from .faces import face_soft_mask, skin_soft_mask

log = logging.getLogger("kestrel.subject")

_HOG = None


def _hog():
    global _HOG
    if _HOG is None:
        try:
            h = cv2.HOGDescriptor()
            h.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            _HOG = h
        except Exception as exc:  # pragma: no cover
            log.warning("HOG people detector unavailable: %s", exc)
            _HOG = False
    return _HOG


def _saliency(rgb: np.ndarray, max_side: int = 900) -> np.ndarray:
    h, w = rgb.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    small = rgb if scale == 1.0 else cv2.resize(rgb, (int(w * scale), int(h * scale)),
                                                interpolation=cv2.INTER_AREA)
    out = np.zeros(small.shape[:2], np.float32)
    try:
        sal = cv2.saliency.StaticSaliencySpectralResidual_create()
        ok, sm = sal.computeSaliency((np.clip(small, 0, 1)[..., ::-1] * 255).astype(np.uint8))
        if ok:
            out = np.maximum(out, cv2.GaussianBlur(sm.astype(np.float32), (0, 0), 2.0))
    except Exception as exc:  # pragma: no cover
        log.debug("saliency failed: %s", exc)
    if out.max() > 0:
        out /= out.max()
    if scale != 1.0:
        out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR)
    return out


def detect_people(rgb: np.ndarray, max_people: int = 12) -> List[Tuple[float, float, float, float, float]]:
    """Coarse full-body detection; boxes are normalised (x, y, w, h, score)."""
    hog = _hog()
    if not hog:
        return []
    h, w = rgb.shape[:2]
    target_w = 720
    scale = min(1.0, target_w / w)
    small = cv2.resize(rgb, (max(64, int(w * scale)), max(64, int(h * scale))),
                       interpolation=cv2.INTER_AREA)
    bgr = (np.clip(small, 0, 1) * 255).astype(np.uint8)[..., ::-1]
    try:
        rects, weights = hog.detectMultiScale(bgr, winStride=(8, 8), padding=(8, 8), scale=1.04)
    except Exception:
        return []
    out = []
    for (x, y, bw, bh), wgt in zip(rects, np.ravel(weights) if len(weights) else []):
        if wgt < 0.15:
            continue
        nx, ny = x / small.shape[1], y / small.shape[0]
        nw, nh = bw / small.shape[1], bh / small.shape[0]
        # HOG boxes are tight around torso+legs; expand to include head and feet.
        ny = max(0.0, ny - nh * 0.12)
        nh = min(1.0 - ny, nh * 1.22)
        out.append((nx, ny, nw, nh, float(min(0.99, wgt))))
    out.sort(key=lambda b: b[4], reverse=True)
    return out[:max_people]


def _box_mask(shape: Tuple[int, int], boxes: List[Tuple[float, float, float, float]],
              soft: float = 0.15) -> np.ndarray:
    h, w = shape
    m = np.zeros((h, w), np.float32)
    for (x, y, bw, bh) in boxes:
        x0, y0 = int(clamp(x, 0, 1) * w), int(clamp(y, 0, 1) * h)
        x1, y1 = int(clamp(x + bw, 0, 1) * w), int(clamp(y + bh, 0, 1) * h)
        if x1 <= x0 or y1 <= y0:
            continue
        m[y0:y1, x0:x1] = 1.0
    if m.max() > 0:
        k = max(3, int(min(h, w) * 0.012) | 1)
        m = cv2.GaussianBlur(m, (k, k), 0)
    return m


def centre_prior(shape: Tuple[int, int], strength: float = 0.3) -> np.ndarray:
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h * 0.55) / (h / 2)) ** 2)
    p = np.exp(-(d ** 2) / 0.9).astype(np.float32)
    return (1.0 - strength) + strength * p


def edge_aware_feather(mask: np.ndarray, rgb: np.ndarray, radius: int = 12,
                       eps: float = 1e-3) -> np.ndarray:
    """Guided filter so mask edges follow real image edges, not blurs."""
    try:
        guide = np.clip(rgb, 0, 1).astype(np.float32)
        out = cv2.ximgproc.guidedFilter(guide=guide, src=mask.astype(np.float32),
                                       radius=int(radius), eps=float(eps))
        return np.clip(out, 0, 1).astype(np.float32)
    except Exception:
        k = max(3, radius * 2 + 1)
        return cv2.GaussianBlur(mask, (k, k), radius * 0.35)


def grabcut_refine(rgb: np.ndarray, prior: np.ndarray, iterations: int = 3,
                   max_side: int = 700) -> np.ndarray:
    h, w = rgb.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    small = cv2.resize(rgb, (max(32, int(w * scale)), max(32, int(h * scale))),
                       interpolation=cv2.INTER_AREA) if scale != 1.0 else rgb
    p = cv2.resize(prior, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_AREA)
    gc = np.full(small.shape[:2], cv2.GC_PR_BGD, np.uint8)
    gc[p > 0.62] = cv2.GC_PR_FGD
    gc[p > 0.88] = cv2.GC_FGD
    gc[p < 0.10] = cv2.GC_BGD
    try:
        bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
        cv2.grabCut((np.clip(small, 0, 1) * 255).astype(np.uint8), gc, None, bgd, fgd,
                    iterations, cv2.GC_INIT_WITH_MASK)
        out = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 1.0, 0.0).astype(np.float32)
        out = cv2.GaussianBlur(out, (0, 0), 1.5)
        # Keep confident original foreground even if GrabCut was conservative.
        out = np.maximum(out, (prior > 0.93).astype(np.float32))
    except Exception as exc:  # pragma: no cover
        log.debug("grabcut failed: %s", exc)
        out = (prior > 0.5).astype(np.float32)
    if scale != 1.0:
        out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR)
    return out


def _keep_subject_components(mask: np.ndarray, faces: List[FaceBox],
                             people: List[Tuple[float, float, float, float, float]],
                             shape: Tuple[int, int]) -> np.ndarray:
    """Keep the main subject component and anything it physically contains.

    Without this, a busy crowd merges into the foreground and every background
    face is treated as the athlete.
    """
    h, w = shape
    m8 = (mask > 0.5).astype(np.uint8)
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(m8, 8)
    if n <= 1:
        return mask
    areas = stats[1:, cv2.CC_STAT_AREA].astype(np.float64)
    order = np.argsort(areas)[::-1]
    keep = np.zeros(n, bool)
    keep[order[0] + 1] = True
    anchors: List[Tuple[float, float, float, float]] = []
    for f in faces:
        anchors.append((f.x, f.y, f.w, f.h))
    for p in people:
        anchors.append((p[0], p[1], p[2], p[3]))
    max_area = areas[order[0]] if areas.size else 1.0
    for i in range(1, n):
        if areas[i - 1] < max_area * 0.12:
            continue
        cx, cy = centroids[i]
        for (ax, ay, aw, ah) in anchors:
            if (ax * w) <= cx <= ((ax + aw) * w) and (ay * h) <= cy <= ((ay + ah) * h):
                keep[i] = True
                break
    out = np.where(keep[labels], mask, 0.0).astype(np.float32)
    return out if out.mean() > 0.005 else mask


def _clean(mask: np.ndarray, min_area_frac: float = 0.0004) -> np.ndarray:
    m8 = (mask > 0.5).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m8, 8)
    if n <= 1:
        return mask
    min_area = max(16, int(mask.size * min_area_frac))
    keep = np.zeros_like(m8)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            keep[labels == i] = 1
    return (mask * keep).astype(np.float32)


def subject_mask(rgb: np.ndarray, faces: List[FaceBox], genre: str,
                 allow_people_detection: bool = True
                 ) -> Tuple[np.ndarray, float, List[str], Optional[List[float]]]:
    """Return (soft mask, confidence, notes, bbox)."""
    h, w = rgb.shape[:2]
    notes: List[str] = []
    sal = _saliency(rgb)
    prior = 0.52 * sal + 0.28 * centre_prior((h, w), 0.35)

    people: List[Tuple[float, float, float, float, float]] = []
    if allow_people_detection and genre in ("sports", "event", "wedding", "portrait", "general"):
        people = detect_people(rgb)
        if people:
            notes.append(f"{len(people)} full-body subject(s) detected")
            boxes = [(p[0], p[1], p[2], p[3]) for p in people]
            prior = np.maximum(prior, 0.72 * _box_mask((h, w), boxes))

    if faces:
        prior = np.maximum(prior, face_soft_mask((h, w), faces, feather=0.6, expand=2.1) * 0.95)
        notes.append(f"{len(faces)} face(s) detected")
        # A large face means the athlete is the subject, not a spectator.  Anchor a
        # body-shaped prior under it: action crops are tight, the torso is cropped,
        # and saliency alone under-covers a player who fills half the frame.
        biggest = max(faces, key=lambda f: f.area)
        if biggest.area >= 0.010:
            fx, fy, fw, fh = biggest.x, biggest.y, biggest.w, biggest.h
            bx0 = clamp(fx - fw * 1.5, 0.0, 1.0)
            bw = clamp(fw * 4.0, 0.05, 1.0 - bx0)
            by0 = clamp(fy - fh * 0.8, 0.0, 1.0)
            bh = clamp(1.0 - by0, 0.05, 1.0)
            prior = np.maximum(prior, 0.85 * _box_mask((h, w), [(bx0, by0, bw, bh)], soft=0.9))
            notes.append("subject anchored on the largest face")

    # Colour contrast: strong chroma/luma difference from the border colour pulls
    # the subject forward (important for team strips against grass).
    border = np.concatenate([rgb[:max(1, h // 20)].reshape(-1, 3), rgb[-max(1, h // 20):].reshape(-1, 3),
                             rgb[:, :max(1, w // 20)].reshape(-1, 3),
                             rgb[:, -max(1, w // 20):].reshape(-1, 3)])
    bc = border.mean(axis=0)
    diff = np.linalg.norm(rgb - bc.reshape(1, 1, 3), axis=2)
    diff = cv2.GaussianBlur(diff, (0, 0), 3)
    dn = diff / max(float(np.percentile(diff, 97)), 1e-4)
    prior = np.clip(prior * (0.72 + 0.4 * np.clip(dn, 0, 1)), 0, 1)

    # Evidence of a large, coherent subject is required before we cut anything out.
    sal_energy = float((sal > 0.45).mean())
    conf = clamp(0.35 + sal_energy * 5.0 + 0.30 * len(people) + 0.18 * min(len(faces), 3), 0.1, 0.95)
    if genre in ("landscape",) and not faces and not people:
        conf = min(conf, 0.35)
    if genre == "sports" and (people or faces):
        conf = max(conf, 0.7)

    if conf < 0.28:
        notes.append("subject uncertain: using gentle centre-weighted treatment")
        soft = np.clip(prior, 0, 1)
        return edge_aware_feather(soft, rgb, 18, 4e-3), conf, notes, None

    prior8 = (np.clip(prior, 0, 1) * 255).astype(np.uint8)
    _, otsu = cv2.threshold(prior8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    binary = (otsu > 0).astype(np.float32)
    seeded = np.clip(0.65 * binary + 0.35 * prior, 0, 1)
    refined = grabcut_refine(rgb, seeded)
    refined = _clean(refined, 0.0003)
    refined = _keep_subject_components(refined, faces, people, (h, w))
    refined = np.maximum(refined, (prior > 0.95).astype(np.float32) * 0.85)
    refined = edge_aware_feather(refined, rgb, radius=max(6, int(min(h, w) * 0.01)), eps=1.2e-3)
    # Grow by roughly one per cent of the frame before feathering: a mask that is
    # a few pixels tight leaves a visible halo when the subject is brightened.
    grow = max(3, int(min(h, w) * 0.008) | 1)
    refined = cv2.dilate(refined, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow, grow)))
    refined = cv2.GaussianBlur(refined, (0, 0), max(1.0, min(h, w) * 0.002))
    refined = np.clip(refined, 0, 1)
    coverage = float(refined.mean())

    ys, xs = np.nonzero(refined > 0.5)
    bbox = None
    if xs.size > 50:
        bbox = [float(xs.min() / w), float(ys.min() / h),
                float((xs.max() - xs.min()) / w), float((ys.max() - ys.min()) / h)]
    return refined, conf, notes, bbox


def grass_mask(rgb: np.ndarray, sky: Optional[np.ndarray] = None) -> np.ndarray:
    h, w = rgb.shape[:2]
    cov, green = scene_mod.green_coverage(rgb)
    hsv = cv2.cvtColor(np.clip(rgb, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
    hue, sat, val = hsv[..., 0] / 360.0, hsv[..., 1], hsv[..., 2]
    # Grass: green-yellow band, needs measurable saturation and mid tone.
    band = color.smoothstep(hue, 0.13, 0.20) * (1.0 - color.smoothstep(hue, 0.43, 0.50))
    m = band * color.smoothstep(sat, 0.06, 0.22) * color.smoothstep(val, 0.08, 0.22)
    if cov < 0.03:
        return np.zeros((h, w), np.float32)
    yy = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    m *= 0.35 + 0.65 * yy
    m = edge_aware_feather(m.astype(np.float32), rgb, 14, 3e-3)
    if sky is not None:
        m *= (1.0 - np.clip(sky, 0, 1) * 0.9)
    return np.clip(m, 0, 1).astype(np.float32)


def sky_mask(rgb: np.ndarray) -> np.ndarray:
    cov, sky = scene_mod.sky_coverage(rgb)
    if cov < 0.02:
        return np.zeros(rgb.shape[:2], np.float32)
    m = _clean(sky, 0.002)
    m = edge_aware_feather(m, rgb, 12, 2.5e-3)
    return np.clip(m, 0, 1).astype(np.float32)


def court_mask(rgb: np.ndarray) -> np.ndarray:
    cov, court = scene_mod.court_coverage(rgb)
    if cov < 0.04:
        return np.zeros(rgb.shape[:2], np.float32)
    m8 = (court > 0.5).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m8, 8)
    keep = np.zeros_like(m8)
    h, w = m8.shape
    for i in range(1, n):
        # a real court is a large, coherent surface — not scattered skin tones
        if stats[i, cv2.CC_STAT_AREA] > 0.05 * h * w:
            keep[labels == i] = 1
    if keep.mean() < 0.04:
        return np.zeros((h, w), np.float32)
    m = _clean((court * keep).astype(np.float32), 0.002)
    m = edge_aware_feather(m, rgb, 14, 3e-3)
    return np.clip(m, 0, 1).astype(np.float32)


def crowd_mask(rgb: np.ndarray, subject: np.ndarray, sky: np.ndarray) -> np.ndarray:
    """Spectator/spectator-like background: textured, busy, not the subject."""
    h, w = rgb.shape[:2]
    gray = color.luma_display(rgb)
    lap = np.abs(cv2.Laplacian(gray, cv2.CV_32F, ksize=3))
    tex = cv2.GaussianBlur(lap, (0, 0), 6)
    tex = tex / max(float(np.percentile(tex, 97)), 1e-5)
    yy = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    band = color.smoothstep(yy, 0.05, 0.25) * (1.0 - color.smoothstep(yy, 0.75, 0.95))
    m = np.clip(tex, 0, 1) * band * (1.0 - subject) * (1.0 - sky)
    return cv2.GaussianBlur(m, (0, 0), 8).astype(np.float32)


def skin_mask(rgb: np.ndarray, faces: List[FaceBox]) -> np.ndarray:
    return skin_soft_mask(rgb, faces)


def clothing_mask(rgb: np.ndarray, subject: np.ndarray, faces: List[FaceBox]) -> Tuple[np.ndarray, List[List[float]]]:
    """Saturated, coherent colour areas *inside* the subject — kit/uniform colours.

    Returns the mask and up to two dominant clothing colours (display RGB).
    """
    if not np.any(subject > 0.5):
        return np.zeros(rgb.shape[:2], np.float32), []
    hsv = cv2.cvtColor(np.clip(rgb, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
    sat, val = hsv[..., 1], hsv[..., 2]
    face = face_soft_mask(rgb.shape[:2], faces, feather=0.5, expand=1.4) if faces else 0
    m = np.clip(subject, 0, 1) * color.smoothstep(sat, 0.28, 0.55) * color.smoothstep(val, 0.06, 0.2)
    m *= (1.0 - np.clip(face, 0, 1))
    if m.sum() < 500:
        return np.zeros(rgb.shape[:2], np.float32), []
    # Cluster the two most common colours among high-confidence pixels.
    px = rgb[m > 0.6]
    if px.shape[0] > 4000:
        px = px[:: px.shape[0] // 4000]
    from .cluster import kmeans_simple
    cols = kmeans_simple(px, k=2 if px.shape[0] > 200 else 1)
    out = []
    for c, reff in cols:
        centre = np.array(c, np.float32).reshape(1, 1, 3)
        d = np.linalg.norm(rgb - centre, axis=2)
        sel = (d < 0.16) & (m > 0.3)
        if sel.sum() > 300 and reff > 0.15:
            out.append([float(x) for x in c])
    return edge_aware_feather(m.astype(np.float32), rgb, 8, 1e-3), out


def region_stats(rgb: np.ndarray, mask: Optional[np.ndarray], kind: str,
                 notes: str = "") -> Optional[SubjectRegion]:
    if mask is None:
        return None
    sel = mask > 0.5
    cov = float(mask.mean())
    if sel.sum() < 100:
        return SubjectRegion(kind=kind, coverage=cov, confidence=0.2, notes=notes)
    px = rgb[sel]
    lab = color.rgb_to_lab(px).reshape(-1, 3)
    ys, xs = np.nonzero(sel)
    h, w = rgb.shape[:2]
    return SubjectRegion(
        kind=kind, coverage=cov,
        mean_luma=float(np.median(color.luma_display(px))),
        mean_rgb=[float(v) for v in px.mean(axis=0)],
        mean_lab=[float(v) for v in lab.mean(axis=0)],
        confidence=0.8 if notes else 0.6,
        bbox=[float(xs.min() / w), float(ys.min() / h),
              float((xs.max() - xs.min()) / w), float((ys.max() - ys.min()) / h)],
        notes=notes,
    )
