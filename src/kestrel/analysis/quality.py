"""Sensor, lens and optical quality analysis.

The measurements here drive three decisions that separate a professional edit
from a naive one:

* how much detail can safely be lifted (noise floor vs. signal),
* how aggressive noise reduction must be (and where),
* how much sharpening an image can take before halos appear.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

from .. import color
from ..core import clamp
from ..models import QualityInfo

IMMERKAER = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)


def _flat_blocks(gray: np.ndarray, block: int = 8, max_blocks: int = 4000):
    h, w = gray.shape
    bh, bw = h // block, w // block
    if bh < 4 or bw < 4:
        return None
    blocks = gray[:bh * block, :bw * block].reshape(bh, block, bw, block)
    blocks = blocks.transpose(0, 2, 1, 3).reshape(-1, block, block)
    if blocks.shape[0] > max_blocks:
        idx = np.linspace(0, blocks.shape[0] - 1, max_blocks).astype(int)
        blocks = blocks[idx]
    return blocks


def estimate_noise(rgb: np.ndarray) -> Dict[str, float]:
    """Noise floor estimate, robust to texture.

    Texture and noise look identical locally, so we do not trust a global residual.
    Instead: split the frame into small blocks, estimate sigma per block with the
    Immerkær operator, then take a low percentile of the block distribution as the
    noise floor — flat, texture-free blocks define the sensor's floor while textured
    blocks sit far above it.  Chroma noise is measured the same way in Lab a*/b*.
    """
    gray = color.luma_display(rgb).astype(np.float32)
    h, w = gray.shape
    block = 8
    bh, bw = h // block, w // block
    if bh < 4 or bw < 4:
        return {"sigma_luma": 0.004, "sigma_chroma": 0.002, "shadow_bias": 0.0,
                "iso_estimate": 200, "flat_fraction": 0.0}
    def immerkaer_abs(img: np.ndarray) -> np.ndarray:
        """|Immerkær response| on the interior pixels (no cv2 required)."""
        k = IMMERKAER
        out = (k[0, 0] * img[0:-2, 0:-2] + k[0, 1] * img[0:-2, 1:-1] + k[0, 2] * img[0:-2, 2:]
               + k[1, 0] * img[1:-1, 0:-2] + k[1, 1] * img[1:-1, 1:-1] + k[1, 2] * img[1:-1, 2:]
               + k[2, 0] * img[2:, 0:-2] + k[2, 1] * img[2:, 1:-1] + k[2, 2] * img[2:, 2:])
        return np.abs(out) * (math.sqrt(math.pi / 2) / 6.0)

    resp = immerkaer_abs(gray)
    rh, rw = resp.shape
    ph, pw = rh // block, rw // block
    if ph < 4 or pw < 4:
        return {"sigma_luma": 0.004, "sigma_chroma": 0.002, "shadow_bias": 0.0,
                "iso_estimate": 200, "flat_fraction": 0.0}
    pooled = resp[:ph * block, :pw * block].reshape(ph, block, pw, block).mean(axis=(1, 3))
    sigma_blocks = pooled.reshape(-1)
    lum_map = gray[1:-1, 1:-1][:ph * block, :pw * block].reshape(ph, block, pw, block).mean(axis=(1, 3))
    lum_blocks = lum_map.reshape(-1)
    floor = float(np.percentile(sigma_blocks, 12))

    lab = color.rgb_to_lab(rgb)
    a_ch = lab[..., 1].astype(np.float32)
    b_ch = lab[..., 2].astype(np.float32)
    chroma_resp = np.hypot(immerkaer_abs(a_ch) / 100.0, immerkaer_abs(b_ch) / 100.0)
    cpool = chroma_resp[:ph * block, :pw * block].reshape(ph, block, pw, block).mean(axis=(1, 3))
    chroma_blocks = cpool.reshape(-1)

    sigma_chroma = float(np.percentile(chroma_blocks, 12))

    dark = sigma_blocks[lum_blocks < 0.22]
    bright = sigma_blocks[lum_blocks > 0.55]
    sd = float(np.percentile(dark, 10)) if dark.size > 30 else floor
    sb = float(np.percentile(bright, 10)) if bright.size > 30 else floor
    shadow_bias = float(clamp((sd / max(sb, 1e-5)) - 1.0, 0.0, 3.0))
    # Empirical calibration: block pooling plus the luma weighting of per-channel
    # noise biases the raw response low, so scale to display-luminance sigma.
    sigma_luma = float(clamp(floor * 1.75, 0.0008, 0.12))
    # Anchors measured on real JPEG/RAW conversions: ISO100 ≈ 0.004, ISO3200 ≈ 0.017.
    iso = int(clamp(round(100 * (sigma_luma / 0.004) ** 2 / 100.0) * 100, 100, 51200))
    flat_fraction = float((sigma_blocks < max(floor * 1.6, 0.002)).mean())
    return {"sigma_luma": sigma_luma, "sigma_chroma": sigma_chroma,
            "shadow_bias": shadow_bias, "iso_estimate": iso,
            "flat_fraction": flat_fraction}


def estimate_sharpness(rgb: np.ndarray, mask: Optional[np.ndarray] = None) -> Dict[str, float]:
    gray = color.luma_display(rgb)
    gray = cv2.GaussianBlur(gray, (0, 0), 0.8)   # suppress noise before measuring
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    if mask is not None and mask.sum() > 500:
        m = mask > 0.5
        vals = mag[m]
        p90 = float(np.percentile(vals, 90))
        p99 = float(np.percentile(vals, 99))
        lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)[m]
        acut = float(np.percentile(np.abs(lap), 85))
    else:
        p90 = float(np.percentile(mag, 90))
        p99 = float(np.percentile(mag, 99))
        lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
        acut = float(np.percentile(np.abs(lap), 85))
    contrast = float(np.percentile(gray, 99) - np.percentile(gray, 1))
    sharpness = clamp(acut / max(contrast, 0.08) / 0.30, 0.0, 1.0)

    # Directional blur from the structure tensor (motion blur confidence)
    win = mask > 0.5 if (mask is not None and mask.sum() > 2000) else np.ones_like(gray, bool)
    gxx, gxy, gyy = gx * gx, gx * gy, gy * gy
    sx = float(gxx[win].mean()) if win.any() else 0.0
    sy = float(gyy[win].mean()) if win.any() else 0.0
    sxy = float(gxy[win].mean()) if win.any() else 0.0
    tr = sx + sy
    det = max(sx * sy - sxy * sxy, 0.0)
    disc = max(tr * tr / 4 - det, 0.0) ** 0.5
    l1, l2 = tr / 2 + disc, max(tr / 2 - disc, 1e-8)
    anisotropy = float(clamp(1.0 - l2 / max(l1, 1e-8), 0.0, 1.0))
    return {"sharpness": float(sharpness), "gradient_p99": p99,
            "acutance": acut, "contrast_range": contrast,
            "anisotropy": anisotropy}


def estimate_compression(rgb: np.ndarray) -> Dict[str, float]:
    gray = color.luma_display(rgb)
    h, w = gray.shape
    if h < 32 or w < 32:
        return {"blockiness": 0.0, "artifacts": 0.0}
    dx = np.abs(np.diff(gray, axis=1))
    dy = np.abs(np.diff(gray, axis=0))
    bx = dx[:, 7::8].mean() if dx.shape[1] > 8 else 0.0
    ix = dx[:, 3::8].mean() if dx.shape[1] > 8 else 0.0
    by = dy[7::8, :].mean() if dy.shape[0] > 8 else 0.0
    iy = dy[3::8, :].mean() if dy.shape[0] > 8 else 0.0
    ratio = (bx + by) / max(ix + iy, 1e-6)
    blockiness = float(clamp(ratio - 1.0, 0.0, 1.0) * 2.0)
    # Banding: repeated flat steps in smooth gradients (posterisation).
    lum = gray.reshape(-1)
    hist = np.bincount(np.clip((lum * 255).astype(np.int32), 0, 255), minlength=256).astype(np.float64)
    hist /= max(hist.sum(), 1)
    occupied = float((hist > 1e-5).mean())
    banding = float(clamp(1.0 - occupied / 0.82, 0.0, 1.0))
    return {"blockiness": blockiness, "artifacts": float(clamp(blockiness * 0.7 + banding * 0.3, 0, 1)),
            "banding": banding}


def estimate_ca(rgb: np.ndarray) -> float:
    """Lateral chromatic aberration proxy (0..1) from colour fringing on edges.

    Fringing is measured only on strong, thin edges and normalised by the local
    luminance gradient, so a high-contrast red jersey or backlit rim light is not
    mistaken for optics.
    """
    scale = min(1.0, 1000.0 / max(rgb.shape[:2]))
    small = cv2.resize(rgb, (max(32, int(rgb.shape[1] * scale)),
                             max(32, int(rgb.shape[0] * scale))),
                       interpolation=cv2.INTER_AREA) if scale < 1.0 else rgb
    r, g, b = small[..., 0], small[..., 1], small[..., 2]
    gray = color.luma_display(small)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    strong = mag > max(float(np.percentile(mag, 98.5)), 0.10)
    if strong.sum() < 300:
        return 0.0
    # high-pass the chroma channels: fringing is a local channel misalignment
    rg = (r - g) - cv2.GaussianBlur(r - g, (0, 0), 1.4)
    bg = (b - g) - cv2.GaussianBlur(b - g, (0, 0), 1.4)
    fringe = np.abs(rg) + np.abs(bg)
    ratio = float(np.median(fringe[strong]) / max(float(np.median(mag[strong])), 1e-3))
    return float(clamp(ratio / 0.55, 0.0, 1.0))


def estimate_vignette(rgb: np.ndarray) -> Tuple[float, float]:
    """Return (corner/centre luma ratio, confidence) for lens vignetting.

    Confidence requires the brightness deficit to be *radially symmetric*: a dark
    background or a stadium crowd is not vignetting, so a one-sided dark area must
    not trigger correction.
    """
    gray = color.luma_display(rgb)
    h, w = gray.shape
    cy, cx = (h - 1) / 2, (w - 1) / 2
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.hypot((yy - cy) / max(cy, 1), (xx - cx) / max(cx, 1)) / math.sqrt(2.0)
    grad = np.hypot(cv2.Sobel(gray, cv2.CV_32F, 1, 0, 3), cv2.Sobel(gray, cv2.CV_32F, 0, 1, 3))
    flat = grad < np.percentile(grad, 60)
    centre = gray[(r < 0.30) & flat]
    if centre.size < 300:
        return 1.0, 0.0
    cmean = float(centre.mean())
    ratios = []
    for name, sel in (("tl", (xx < w * 0.22) & (yy < h * 0.22)),
                      ("tr", (xx > w * 0.78) & (yy < h * 0.22)),
                      ("bl", (xx < w * 0.22) & (yy > h * 0.78)),
                      ("br", (xx > w * 0.78) & (yy > h * 0.78))):
        patch = gray[sel & flat]
        if patch.size > 120:
            ratios.append(float(patch.mean() / max(cmean, 1e-4)))
    if len(ratios) < 3:
        return 1.0, 0.0
    ratios = np.array(ratios)
    mean_ratio = float(ratios.mean())
    spread = float(ratios.std())
    # All four corners must agree for this to be optics rather than content.
    sym = clamp(1.0 - spread / 0.18, 0.0, 1.0)
    deficit = clamp(1.0 - mean_ratio, 0.0, 0.5)
    conf = float(sym * clamp(deficit / 0.06, 0.0, 1.0))
    ratio = float(clamp(mean_ratio, 0.55, 1.05)) if conf > 0.2 else 1.0
    return ratio, conf


def analyse_quality(rgb: np.ndarray, meta: Any, subject_mask: Optional[np.ndarray] = None
                    ) -> QualityInfo:
    noise = estimate_noise(rgb)
    sharp = estimate_sharpness(rgb, subject_mask)
    comp = estimate_compression(rgb)
    ca = estimate_ca(rgb)
    vign, vconf = estimate_vignette(rgb)
    q = QualityInfo(
        noise_sigma=noise["sigma_luma"] * 100.0,   # stored in 0..100 slider-like units
        iso_estimate=noise["iso_estimate"],
        noise_luma=noise["sigma_luma"] * 100.0,
        noise_chroma=noise["sigma_chroma"] * 100.0,
        noise_shadow_bias=noise["shadow_bias"],
        sharpness=sharp["sharpness"],
        motion_blur=float(clamp((sharp["anisotropy"] - 0.62) / 0.3, 0, 1)),
        compression_artifacts=comp["artifacts"],
        banding=comp["banding"],
        bit_depth=getattr(meta, "bit_depth", 8),
        is_raw=bool(getattr(meta, "is_raw", False)),
        camera={"name": getattr(meta, "camera", ""), "lens": getattr(meta, "lens", ""),
                "iso": getattr(meta, "iso", 0), "aperture": getattr(meta, "aperture", "")},
        lens={"ca": ca, "vignette_ratio": vign, "vignette_confidence": vconf,
              "focal_length": getattr(meta, "focal_length", ""),
              "name": getattr(meta, "lens", "")},
    )
    # A simple, explainable quality score (not a "beauty" score).
    bits = 1.0 if q.is_raw else (0.85 if q.bit_depth == 16 else 0.7)
    noise_pen = clamp(q.noise_sigma / 6.0, 0, 1) * 0.35
    sharp_bonus = (q.sharpness - 0.35) * 0.25
    q.quality_score = float(clamp(bits * 0.6 + sharp_bonus + (1 - noise_pen) * 0.3, 0.05, 1.0))
    return q


def noise_map(rgb: np.ndarray) -> np.ndarray:
    """Per-pixel relative noise weight (higher in shadows and flat areas)."""
    gray = color.luma_display(rgb)
    local = cv2.GaussianBlur(gray * gray, (0, 0), 2.5) - cv2.GaussianBlur(gray, (0, 0), 2.5) ** 2
    local = np.sqrt(np.maximum(local, 0))
    shadow = 1.0 - color.smoothstep(gray, 0.05, 0.55)
    return (0.25 * 0.0 + local / (local.max() + 1e-4) * 0.35 + shadow * 0.65).astype(np.float32)
