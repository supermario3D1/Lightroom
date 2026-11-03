"""Scene, lighting and genre understanding.

No single cue is trusted.  Lighting and genre are *scored* from many independent
signals (colour, geometry, faces, texture, noise) and the margin between the top
genres becomes the confidence shown in the UI.  When the signals disagree the
engine deliberately falls back to conservative, genre-neutral editing rather
than guessing.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from .. import color
from ..core import clamp
from ..models import FaceBox, SceneInfo

GENRES = ["sports", "wedding", "portrait", "event", "landscape", "wildlife",
          "commercial", "general"]

GENRE_LABEL = {
    "sports": "Sports", "wedding": "Wedding", "portrait": "Portrait",
    "event": "Event", "landscape": "Landscape", "wildlife": "Wildlife",
    "commercial": "Commercial", "general": "General",
}


def _hsv(rgb: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """HSV with hue normalised to 0..1 (OpenCV float HSV uses 0..360)."""
    hsv = cv2.cvtColor(np.clip(rgb, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
    return hsv[..., 0] / 360.0, hsv[..., 1], hsv[..., 2]


def green_coverage(rgb: np.ndarray) -> Tuple[float, np.ndarray]:
    h, s, v = _hsv(rgb)
    green = ((h > 0.18) & (h < 0.46) & (s > 0.10) & (v > 0.10)).astype(np.float32)
    return float(green.mean()), green


REGION_EDGE = 900  # region cues are measured at a fixed scale (see _cue_image)


def _cue_image(rgb: np.ndarray, edge: int = REGION_EDGE) -> np.ndarray:
    """Downscale to a fixed long edge before measuring coverage.

    Coverage is a *fraction of the frame*, so it must not change when the same
    photograph is analysed at a different preview size.  Measuring the cues on
    a fixed working scale also makes the numbers comparable between a 12 MP
    JPEG and a 45 MP RAW.
    """
    hh, ww = rgb.shape[:2]
    scale = min(1.0, edge / max(hh, ww))
    if scale >= 1.0:
        return rgb
    return cv2.resize(rgb, (max(32, int(ww * scale)), max(32, int(hh * scale))),
                      interpolation=cv2.INTER_AREA)


def _texture(rgb: np.ndarray) -> np.ndarray:
    """Local detail energy normalised to 0..1 (floors are smooth, crowds are not)."""
    gray = color.luma_display(rgb)
    lap = np.abs(cv2.Laplacian(gray, cv2.CV_32F, ksize=3))
    tex = cv2.GaussianBlur(lap, (0, 0), 4.0)
    return np.clip(tex / max(float(np.percentile(tex, 98)), 1e-5), 0, 1)


def court_coverage(rgb: np.ndarray) -> Tuple[float, np.ndarray]:
    """Basketball/hardwood and painted court surfaces (not grass).

    A real playing surface is a *large, smooth, lower-frame* region: hardwood
    (warm tan), painted key (dark saturated blue/red/green) or a pale synthetic
    floor.  Isolated warm patches — skin, wooden furniture, a tan wall — are
    rejected because they never form one component covering several per cent of
    the frame.
    """
    hh, ww = rgb.shape[:2]
    small = _cue_image(rgb)
    h, s, v = _hsv(small)
    sh, sw = small.shape[:2]
    yy = np.linspace(0, 1, sh, dtype=np.float32)[:, None]
    lower = color.smoothstep(yy, 0.34, 0.58)
    tex = _texture(small)
    smooth = tex < 0.30
    r, g, b = small[..., 0], small[..., 1], small[..., 2]
    green_dominant = (g > r * 1.02) & (g > b * 1.10)
    wood = ((h > 0.015) & (h < 0.12) & (s > 0.14) & (s < 0.85)
            & (v > 0.16) & (v < 0.95)) & ~green_dominant
    pale = smooth & (s < 0.30) & (v > 0.22) & (v < 0.88)
    floor_px = (wood | pale).astype(np.float32) * lower * (0.35 + 0.65 * smooth)

    m8 = (floor_px > 0.55).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m8, 8)
    keep = np.zeros_like(m8)
    min_area = 0.04 * sh * sw
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            keep[labels == i] = 1
    cov = float(keep.mean())
    if cov < 0.03:
        return 0.0, np.zeros((hh, ww), np.float32)
    mask = floor_px * (0.45 + 0.55 * keep)
    mask = cv2.GaussianBlur(mask, (0, 0), 2.0)
    if (sh, sw) != (hh, ww):
        mask = cv2.resize(mask, (ww, hh), interpolation=cv2.INTER_LINEAR)
    return cov, np.clip(mask, 0, 1).astype(np.float32)


def arena_cues(rgb: np.ndarray) -> Dict[str, float]:
    """Crowd / floor / kit signature of a live venue.

    These three cues together are what separates a basketball arena from a
    street scene or a portrait: a busy band of spectators, a smooth reflective
    floor, and large areas of one saturated team colour.  Each is reported
    0..1 so the genre scorer can weigh them, and none is decisive alone.
    """
    small = _cue_image(rgb)
    sh, sw = small.shape[:2]
    h, s, v = _hsv(small)
    tex = _texture(small)
    yy = np.linspace(0, 1, sh, dtype=np.float32)[:, None]
    upper = 1.0 - color.smoothstep(yy, 0.30, 0.58)
    lower = color.smoothstep(yy, 0.58, 0.78)
    crowd = float(((tex * upper) > 0.45).mean())
    floor = float((tex * lower < 0.22).mean())
    kit = float((s > 0.40).mean())
    # A single dominant hue among saturated pixels is the signature of a team
    # strip (two kits at most); a wedding or street scene spreads across hues.
    sat_px = s > 0.40
    concentration = 0.0
    if float(sat_px.mean()) > 0.02:
        hues = h[sat_px]
        hist, _ = np.histogram(hues, bins=18, range=(0, 1))
        concentration = float(hist.max() / max(hist.sum(), 1))
    return {"crowd": crowd, "floor": floor, "kit": kit, "hue_concentration": concentration}


def sky_coverage(rgb: np.ndarray) -> Tuple[float, np.ndarray]:
    """Sky: smooth, blue/cyan (or bright overcast) and connected to the top edge.

    The smoothness test matters: a bright arena ceiling, a scoreboard or an LED
    band is high-contrast and must not be read as sky — otherwise an indoor
    court photograph is edited as a landscape.
    """
    hh, ww = rgb.shape[:2]
    small = _cue_image(rgb)
    sh, sw = small.shape[:2]
    h, s, v = _hsv(small)
    tex = _texture(small)
    yy = np.linspace(0, 1, sh, dtype=np.float32)[:, None]
    upper = 1.0 - color.smoothstep(yy, 0.45, 0.62)
    smooth = tex < 0.28
    blue = (h > 0.48) & (h < 0.72) & (s > 0.08) & (v > 0.30)
    bright = (v > 0.72) & (s < 0.16)
    cand = ((blue | bright) & smooth & (upper > 0.25)).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(cand, 8)
    keep = np.zeros_like(cand)
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if area >= 0.02 * sh * sw and y <= 2:
            keep[labels == i] = 1
    if keep.mean() < 0.02:
        return 0.0, np.zeros((hh, ww), np.float32)
    mask = cv2.GaussianBlur(keep.astype(np.float32), (0, 0), 2.0)
    if (sh, sw) != (hh, ww):
        mask = cv2.resize(mask, (ww, hh), interpolation=cv2.INTER_LINEAR)
    return float(keep.mean()), np.clip(mask, 0, 1).astype(np.float32)


def white_fabric_coverage(rgb: np.ndarray, faces: List[FaceBox]) -> float:
    """Near-white, low-saturation areas below faces (wedding dress heuristic)."""
    v = np.clip(rgb, 0, 1).max(axis=2)
    mn = np.clip(rgb, 0, 1).min(axis=2)
    sat = np.where(v > 1e-4, (v - mn) / np.maximum(v, 1e-4), 0)
    white = ((v > 0.72) & (sat < 0.13)).astype(np.float32)
    hh, ww = white.shape
    yy = np.linspace(0, 1, hh, dtype=np.float32)[:, None]
    lower = np.clip((yy - 0.35) / 0.3, 0, 1)
    white *= lower
    return float(white.mean())


def near_neutral_blocks(rgb: np.ndarray, grid: int = 12) -> Tuple[np.ndarray, np.ndarray]:
    """Per-block mean colour of near-neutral, well-exposed pixels.

    Returns (rgains Nx2 in r/b relative to g, weights N).  Used to detect mixed
    lighting: real mixed light produces *different* white balances per region.
    """
    hh, ww = rgb.shape[:2]
    means, weights = [], []
    for by in range(grid):
        for bx in range(grid):
            y0, y1 = int(by * hh / grid), int((by + 1) * hh / grid)
            x0, x1 = int(bx * ww / grid), int((bx + 1) * ww / grid)
            patch = rgb[y0:y1, x0:x1].reshape(-1, 3)
            if patch.shape[0] < 200:
                continue
            mx = patch.max(axis=1)
            mn = patch.min(axis=1)
            sat = np.where(mx > 1e-4, (mx - mn) / np.maximum(mx, 1e-4), 0)
            sel = (mx > 0.18) & (mx < 0.92) & (sat < 0.22)
            if sel.sum() < 120:
                continue
            m = patch[sel].mean(axis=0) + 1e-4
            means.append([m[0] / m[1], m[2] / m[1]])
            weights.append(float(sel.sum()))
    if not means:
        return np.zeros((0, 2), np.float32), np.zeros(0, np.float32)
    return np.asarray(means, np.float32), np.asarray(weights, np.float32)


def estimate_horizon_tilt(rgb: np.ndarray, sky_mask: np.ndarray) -> float:
    if sky_mask.mean() < 0.02:
        return 0.0
    hh, ww = sky_mask.shape
    small = cv2.resize(sky_mask, (min(ww, 480), min(hh, 320)), interpolation=cv2.INTER_AREA)
    edges = cv2.Canny((small * 255).astype(np.uint8), 40, 120)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=45, minLineLength=small.shape[1] // 3,
                            maxLineGap=12)
    if lines is None:
        return 0.0
    angles = []
    for l in lines[:, 0]:
        x1, y1, x2, y2 = l
        a = math.degrees(math.atan2(y2 - y1, x2 - x1))
        if abs(a) < 20:
            angles.append(a)
    if not angles:
        return 0.0
    return float(clamp(np.median(angles), -15, 15))


def analyse_lighting(rgb: np.ndarray, scene: SceneInfo, faces: List[FaceBox],
                     subject: Optional[np.ndarray], background: Optional[np.ndarray],
                     sky_cov: float, green_cov: float, court_cov: float,
                     noise_iso: int) -> SceneInfo:
    gray = color.luma_display(rgb)
    mean_l = float(np.mean(gray))
    p05, p50, p95 = [float(np.percentile(gray, q)) for q in (5, 50, 95)]
    contrast = p95 - p05
    h, s, v = _hsv(rgb)
    mean_sat = float(np.mean(s[v > 0.05])) if np.any(v > 0.05) else 0.0
    highlights = float((gray > 0.97).mean())

    sky_cov = float(sky_cov)
    crowd_cov = float(scene.crowd_coverage)
    indoor = clamp(0.78 - sky_cov * 3.4 + (0.10 if mean_l < 0.30 else 0.0), 0.02, 0.96)
    # A green/court surface or a dense crowd band means we are in a venue, not a room:
    # floodlit stadiums have no sky in frame but are emphatically outdoors.
    if green_cov > 0.12 or court_cov > 0.12:
        indoor = min(indoor, 0.30)
    if crowd_cov > 0.06 and green_cov > 0.05:
        indoor = min(indoor, 0.18)
    if p95 > 0.85 and mean_l < 0.42:
        indoor = min(indoor, 0.35)
    outdoor = clamp(1.0 - indoor, 0.02, 0.98)

    night = clamp((0.28 - mean_l) / 0.22, 0.0, 1.0) * clamp((p95 - p50) / 0.35, 0.2, 1.2)
    dark_frac = float((gray < 0.08).mean())
    p10 = float(np.percentile(gray, 10))
    # Floodlit night sport: most of the frame is deep shadow while lit subjects and
    # white kit are bright — that signature is more reliable than mean luminance.
    if dark_frac > 0.20 and p95 > 0.55:
        night = max(night, clamp((dark_frac - 0.16) * 2.0, 0.0, 0.8))
    # Deep shadows plus bright surfaces is artificial light even when the mean
    # luminance is only slightly below the daylight threshold.
    if p10 < 0.08 and p95 > 0.55:
        night = max(night, 0.56)
    if p95 > 0.9 and mean_l < 0.3:
        night = max(night, 0.55)
    artificial = clamp(0.15 + night * 0.55 + (0.35 if indoor > 0.6 else 0.0)
                       + (0.25 if noise_iso >= 1600 else 0.0), 0, 1)

    # Mixed lighting from per-region white balance disagreement
    means, weights = near_neutral_blocks(rgb)
    mixed = 0.0
    if means.shape[0] >= 8:
        # Robust spread: a single coloured LED band or a patch of kit must not
        # read as "mixed lighting".  Compare each block's chromaticity to the
        # median of the frame, then use the 75th percentile of that error.
        med = np.median(means, axis=0)
        err = np.abs(means - med).sum(axis=1)
        spread = float(np.percentile(err, 75))
        mixed = clamp((spread - 0.045) / 0.16, 0.0, 1.0)

    # Backlit: subject measurably darker than the background behind it
    backlit = 0.0
    if subject is not None and background is not None:
        sm = float(gray[subject > 0.5].mean()) if (subject > 0.5).any() else mean_l
        bm = float(gray[background > 0.5].mean()) if (background > 0.5).any() else mean_l
        if bm > sm + 0.10 and bm > 0.45:
            backlit = clamp((bm - sm) / 0.35, 0.0, 1.0)

    overcast = clamp((0.22 - contrast) / 0.18, 0.0, 1.0) * clamp(sky_cov * 4.0 +
                                                               (0.5 if indoor < 0.4 else 0.0), 0, 1)
    if mean_sat < 0.12 and contrast < 0.25:
        overcast = max(overcast, 0.3)

    stadium = clamp(green_cov * 1.8 + (0.25 if night > 0.4 else 0.0) +
                    (0.2 if indoor < 0.4 else 0.0) +
                    (0.15 if contrast > 0.55 else 0.0) - 0.35, 0.0, 1.0)
    if court_cov > 0.15:
        stadium = max(stadium, clamp(0.45 + indoor * 0.4, 0, 1))

    scene.indoor = float(indoor)
    scene.outdoor = float(outdoor)
    scene.night = float(night)
    scene.artificial = float(artificial)
    scene.stadium = float(stadium)
    scene.mixed_light = float(mixed)
    scene.backlit = float(backlit)
    scene.overcast = float(overcast)
    scene.crowd_coverage = float(scene.crowd_coverage)
    scene.grass_coverage = float(green_cov)
    scene.court_coverage = float(court_cov)
    scene.sky_coverage = float(sky_cov)

    tags = []
    if night > 0.45:
        tags.append("night")
    if indoor > 0.6:
        tags.append("indoor")
    elif outdoor > 0.6:
        tags.append("outdoor")
    if stadium > 0.5:
        tags.append("stadium")
    if artificial > 0.6 and night < 0.45:
        tags.append("artificial light")
    if mixed > 0.45:
        tags.append("mixed lighting")
    if backlit > 0.35:
        tags.append("backlit")
    if overcast > 0.45:
        tags.append("overcast")
    if highlights > 0.02:
        tags.append("specular highlights")
    scene.event_tags = tags
    return scene


def action_camera_prior(meta) -> float:
    """How strongly the capture settings say "sport".

    Long lens + fast shutter is the most reliable sports signal available and it
    costs nothing: it comes from EXIF, not from a guess about the picture.  It is
    deliberately capped at 0.8 because a long lens is also used for wildlife and
    a fast shutter for children running.
    """
    if meta is None:
        return 0.0
    score = 0.0
    try:
        focal = float(str(meta.focal_length or "").lower().replace("mm", "").strip() or 0)
    except Exception:
        focal = 0.0
    if focal >= 135:
        score += 0.40
    elif focal >= 85:
        score += 0.22
    shutter = 0.0
    raw = str(meta.exposure_time or "").lower()
    try:
        if "/" in raw:
            num, den = raw.split("/", 1)
            shutter = float(num) / float(den)
        elif raw:
            shutter = float(raw)
    except Exception:
        shutter = 0.0
    if 0 < shutter <= 1 / 800:
        score += 0.30
    elif 0 < shutter <= 1 / 400:
        score += 0.15
    try:
        iso = int(meta.iso or 0)
    except Exception:
        iso = 0
    if iso >= 1600:
        score += 0.10
    return float(clamp(score, 0.0, 0.8))


def score_genres(rgb: np.ndarray, faces: List[FaceBox], scene: SceneInfo,
                 subject_coverage: float, subject_confidence: float,
                 sharpness: float, compression: float,
                 white_fabric: float = 0.0,
                 arena: Optional[Dict[str, float]] = None,
                 camera_prior: float = 0.0) -> Dict[str, float]:
    gray = color.luma_display(rgb)
    mean_l = float(gray.mean())
    contrast = float(np.percentile(gray, 95) - np.percentile(gray, 5))
    h, s, v = _hsv(rgb)
    sat = float(np.mean(s[v > 0.05])) if np.any(v > 0.05) else 0.0
    face_area = sum(f.area for f in faces)
    max_face = max([f.area for f in faces], default=0.0)
    # A crowd is made of *small* faces.  Counting every box would make every
    # arena photograph look like an event, so the score follows the size of the
    # biggest face plus a little evidence from the number of faces.
    people = clamp(max_face * 6.0 + 0.25 * clamp(len(faces) / 6.0, 0, 1)
                   + (0.30 if subject_confidence > 0.6 else 0.0), 0, 1)
    green = scene.grass_coverage
    court = scene.court_coverage
    sky = scene.sky_coverage
    arena = arena or {}
    crowd_band = float(arena.get("crowd", 0.0))
    floor_band = float(arena.get("floor", 0.0))
    kit = float(arena.get("kit", 0.0))
    hue_conc = float(arena.get("hue_concentration", 0.0))
    venue = clamp(1.1 * crowd_band
                  + 0.50 * max(0.0, hue_conc - 0.40) / 0.40
                  + 0.35 * max(0.0, kit - 0.15) / 0.50
                  + 0.30 * clamp(len(faces) / 6.0, 0, 1)
                  - 0.42, 0, 1)

    scores: Dict[str, float] = {}
    scores["sports"] = clamp(
        0.26 * clamp(green / 0.25, 0, 1)
        + 0.20 * clamp(court / 0.12, 0, 1)
        + 0.18 * venue
        + 0.10 * clamp(len(faces) / 8.0, 0, 1)     # a crowd of people, not one face
        + 0.06 * max(0.0, kit - 0.15) / 0.5        # team kit is a large flat colour
        + 0.12 * people
        + 0.08 * clamp(scene.stadium, 0, 1)
        + 0.06 * clamp((sat - 0.2) / 0.35, 0, 1)
        + 0.05 * clamp(contrast / 0.6, 0, 1)
        + 0.05 * clamp(subject_confidence - 0.3, 0, 1)
        + camera_prior, 0, 1)

    scores["wedding"] = clamp(
        0.26 * people + 0.24 * clamp(white_fabric / 0.10, 0, 1)
        + 0.18 * clamp((mean_l - 0.32) / 0.3, 0, 1)
        + 0.14 * clamp((1 - contrast) * 1.6, 0, 1)
        + 0.10 * clamp(1 - scene.night, 0, 1)
        + 0.08 * clamp(scene.indoor, 0, 1), 0, 1) * clamp(len(faces) / 2.0, 0, 1)

    scores["portrait"] = clamp(
        0.42 * clamp(max_face / 0.035, 0, 1)
        + 0.20 * clamp(face_area * 5, 0, 1)
        + 0.12 * (1 - scene.night * 0.6)
        + 0.10 * clamp(1 - abs(mean_l - 0.5) / 0.35, 0, 1)
        + 0.08 * clamp(subject_confidence, 0, 1)
        + 0.08 * clamp(1 - green * 3, 0, 1), 0, 1) * clamp(len(faces), 0, 1)

    scores["event"] = clamp(
        0.24 * people + 0.22 * clamp(scene.indoor, 0, 1)
        + 0.10 * clamp(scene.mixed_light, 0, 1)
        + 0.12 * clamp(scene.artificial, 0, 1)
        + 0.05 * clamp(1 - max_face / 0.06, 0, 1)
        + 0.08 * clamp(1 - green * 2, 0, 1)
        + 0.08 * clamp(1 - sky * 2, 0, 1), 0, 1)

    scores["landscape"] = clamp(
        0.34 * clamp(sky / 0.18, 0, 1) * (1 if face_area < 0.01 else 0.35)
        + 0.26 * clamp(green / 0.3, 0, 1) * (1 if face_area < 0.01 else 0.4)
        + 0.18 * (1 if not faces else 0.2)
        + 0.10 * clamp(scene.outdoor, 0, 1)
        + 0.06 * clamp(contrast, 0, 1)
        + 0.06 * clamp(1 - subject_coverage, 0, 1), 0, 1) * (1.0 if court < 0.05 else 0.3)

    scores["wildlife"] = clamp(
        0.28 * clamp(1 - face_area * 20, 0, 1)
        + 0.22 * clamp(subject_confidence, 0, 1)
        + 0.16 * clamp(green / 0.35, 0, 1)
        + 0.14 * clamp(scene.outdoor, 0, 1)
        + 0.10 * clamp((sharpness - 0.3) / 0.5, 0, 1)
        + 0.10 * clamp(1 - compression * 2, 0, 1), 0, 1)
    scores["wildlife"] *= 0.55  # conservative: no animal classifier available

    scores["commercial"] = clamp(
        0.26 * clamp(white_fabric / 0.15, 0, 1)
        + 0.20 * clamp(1 - people, 0, 1)
        + 0.18 * clamp((mean_l - 0.55) / 0.3, 0, 1)
        + 0.16 * clamp(1 - sat * 2, 0, 1)
        + 0.10 * clamp(subject_coverage, 0, 1)
        + 0.10 * clamp(scene.indoor, 0, 1), 0, 1)

    scores["general"] = 0.32
    return {k: float(round(v, 4)) for k, v in scores.items()}


def choose_genre(scores: Dict[str, float], forced: Optional[str] = None
                 ) -> Tuple[str, float]:
    if forced and forced != "auto":
        return forced, 1.0
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, second = ranked[0], ranked[1]
    # Require a real margin before claiming a specialist genre.
    margin = top[1] - second[1]
    conf = clamp(0.42 + margin * 2.4, 0.0, 0.97)
    if top[1] < 0.34:
        return "general", clamp(0.5 + margin, 0, 0.9)
    if margin < 0.035:
        # ambiguous: prefer the more conservative genre of the two
        order = {"wedding": 0, "portrait": 1, "sports": 2, "event": 3,
                 "commercial": 4, "landscape": 5, "wildlife": 6, "general": 7}
        pick = min([top[0], second[0]], key=lambda g: order.get(g, 9))
        return pick, clamp(0.35 + margin, 0, 0.6)
    return top[0], conf
