"""Kestrel render engine — a physically-motivated, high-bit-depth develop pipeline.

The engine is independent of any single AI model: analysis produces an
:class:`EditPlan` (Lightroom-unit adjustments plus soft masks), and this module
turns that plan into pixels.  Editing decisions are therefore fully separable
from rendering, and every step can be explained, unit-tested and replaced.

Order of operations (chosen to match how a professional retoucher works):

1. decoder output (linear light where possible, display sRGB otherwise)
2. optical corrections — lateral CA, vignetting
3. white balance, exposure
4. luminance-only tone mapping — highlight recovery, shadows, whites, blacks,
   contrast; chromaticity is preserved and out-of-gamut colour is soft-compressed
5. tone curve / colour grading
6. dehaze
7. noise reduction (before any detail work)
8. local contrast (texture, clarity) with halo limiting
9. colour — vibrance, saturation, targeted HSL, skin protection
10. local masks (subject, faces, background, sky, grass, crowd…)
11. multi-scale sharpening with edge masking and per-mask detail weighting
12. finishing — vignette, grain, gamut clamp
"""
from __future__ import annotations

import json

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from .. import color
from ..core import clamp
from ..models import EditPlan, GlobalAdjust, MaskLayer

LUMA = color.LUMA.astype(np.float32)


# --------------------------------------------------------------------------- helpers
def _lum(img: np.ndarray) -> np.ndarray:
    return (img[..., 0] * LUMA[0] + img[..., 1] * LUMA[1] + img[..., 2] * LUMA[2])


def _smoothstep(x: np.ndarray, a: float, b: float) -> np.ndarray:
    return color.smoothstep(x, a, b)


def _lin_to_srgb_ext(x: np.ndarray) -> np.ndarray:
    """sRGB transfer without clipping — keeps highlight headroom for recovery."""
    x = np.maximum(x, 0.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def _srgb_to_lin_ext(x: np.ndarray) -> np.ndarray:
    x = np.maximum(x, 0.0)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _guided(guide: np.ndarray, src: np.ndarray, radius: int, eps: float) -> np.ndarray:
    try:
        g = guide if guide.ndim == 3 else guide
        return cv2.ximgproc.guidedFilter(guide=np.ascontiguousarray(g, np.float32),
                                        src=np.ascontiguousarray(src, np.float32),
                                        radius=int(radius), eps=float(eps))
    except Exception:
        k = max(3, int(radius) * 2 + 1)
        return cv2.GaussianBlur(src.astype(np.float32), (k, k), radius * 0.45)


def _resize_to(img: np.ndarray, shape: Tuple[int, int]) -> np.ndarray:
    if img.shape[:2] == shape[:2]:
        return img
    return cv2.resize(img, (shape[1], shape[0]), interpolation=cv2.INTER_LINEAR)


def gamut_compress(img: np.ndarray, threshold: float = 0.985) -> np.ndarray:
    """Desaturate only wildly out-of-gamut pixels instead of hard-clipping hues."""
    mx = img.max(axis=2)
    over = mx > threshold
    if not np.any(over):
        return np.clip(img, 0, 1)
    out = np.clip(img, 0, None)
    lum = _lum(out)
    excess = np.clip((mx - threshold) / np.maximum(mx, 1e-4), 0, 1)
    blended = out * (1 - excess[..., None]) + lum[..., None] * excess[..., None]
    return np.clip(blended, 0, 1)


def _scale_luma(img: np.ndarray, new_lum: np.ndarray) -> np.ndarray:
    old = np.maximum(_lum(img), 1e-5)
    k = (new_lum / old)[..., None]
    out = img * k
    # preserve hue while compressing super-saturated results
    return gamut_compress(out)


@dataclass
class RenderReport:
    ms_total: float = 0.0
    ms_stages: Dict[str, float] = field(default_factory=dict)
    width: int = 0
    height: int = 0
    passes: int = 1
    warnings: List[str] = field(default_factory=list)
    mechanical: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {"ms_total": round(self.ms_total, 1),
                "ms_stages": {k: round(v, 1) for k, v in self.ms_stages.items()},
                "width": self.width, "height": self.height, "passes": self.passes,
                "warnings": self.warnings, "mechanical": self.mechanical}


@dataclass
class RenderConfig:
    quality: str = "high"
    noise_refine: bool = True
    deconvolution: bool = False
    max_edge: Optional[int] = None
    bit_depth: int = 8
    preserve_profile: bool = True


# --------------------------------------------------------------------------- pipeline
class Renderer:
    def __init__(self, config: Optional[RenderConfig] = None):
        self.config = config or RenderConfig()
        # Masks are reused several times per render (local adjustments, sharpening
        # weights); building each one once keeps previews interactive.
        self._mask_cache: Dict[str, np.ndarray] = {}

    # ---------------------------------------------------------------- public
    def render(self, rgb_display: np.ndarray, plan: EditPlan,
               masks: Optional[Dict[str, np.ndarray]] = None,
               meta: Optional[object] = None) -> Tuple[np.ndarray, RenderReport]:
        t_start = time.perf_counter()
        self._mask_cache.clear()
        report = RenderReport(passes=max(1, int(plan.iterations)))
        stages: Dict[str, float] = {}
        img = np.clip(np.asarray(rgb_display, np.float32), 0, 1)
        if self.config.max_edge and max(img.shape[:2]) > self.config.max_edge:
            img = _resize_to(img, (int(img.shape[0] * self.config.max_edge / max(img.shape[:2])),
                                   int(img.shape[1] * self.config.max_edge / max(img.shape[:2]))))
        masks = masks or {}
        shape = img.shape[:2]

        def stage(name: str, fn, *args, **kw):
            t0 = time.perf_counter()
            out = fn(*args, **kw)
            stages[name] = stages.get(name, 0.0) + (time.perf_counter() - t0) * 1000.0
            return out

        # 1-2. optical corrections
        lin = color.srgb_to_linear(img)
        if plan.global_.remove_ca and isinstance(meta, dict):
            ca = float((meta.get("lens") or {}).get("ca", 0.0))
            if ca > 0.22 and self.config.quality == "maximum":
                lin = stage("ca", self._correct_lateral_ca, lin)
        if plan.global_.lens_corrections and isinstance(meta, dict):
            vig = float((meta.get("lens") or {}).get("vignette_ratio", 1.0))
            vconf = float((meta.get("lens") or {}).get("vignette_confidence", 0.0))
            if vig < 0.92 and vconf > 0.25:
                lin = stage("vignette_fix", self._correct_vignette, lin, vig)

        # 3. white balance + exposure
        g = plan.global_
        if g.temperature is not None:
            temp = float(g.temperature)
            tint = float(g.tint if g.tint is not None else 0.0)
            lin = stage("white_balance", self._white_balance, lin, temp, tint)
        exposure = 2.0 ** float(g.exposure)
        lin = np.maximum(lin * exposure, 0.0)

        # 4. luminance-only tone mapping
        toned = stage("tone", self._tone_map, lin, g)
        stages["exposure"] = stages.get("exposure", 0.0)

        # 5. curve / grading
        if g.curve_rgb or g.curve_red or g.curve_blue:
            toned = stage("curve", self._apply_curves, toned, g)

        # 6. dehaze
        if abs(g.dehaze) > 0.5:
            toned = stage("dehaze", self._dehaze, toned, g.dehaze / 100.0)

        # 7. noise reduction
        if (g.luminance_nr > 0.5 or g.color_nr > 0.5) and self.config.noise_refine:
            toned = stage("denoise", self._denoise, toned, g,
                          float(plan.denoise.get("sigma", 0.0)))

        # 8. local contrast
        if abs(g.texture) > 0.5 or abs(g.clarity) > 0.5:
            toned = stage("local_contrast", self._local_contrast, toned, g.texture, g.clarity)

        # 9. colour
        if abs(g.vibrance) > 0.5 or abs(g.saturation) > 0.5 or any(
                abs(v) > 0.5 for v in list(g.hsl_hue.values()) + list(g.hsl_saturation.values()) +
                list(g.hsl_luminance.values())):
            toned = stage("colour", self._colour, toned, g,
                         float(plan.detail.get("skin_protection", 0.85)))

        # 10. local masks
        if plan.masks:
            toned = stage("masks", self._apply_masks, toned, plan, masks)

        # 11. sharpening (+ optional deconvolution at maximum quality)
        if self.config.deconvolution and plan.sharpening.get("deconvolve"):
            toned = stage("deconvolve", self._deconvolve, toned, plan)

        if g.sharpness > 0.5:
            toned = stage("sharpen", self._sharpen, toned, plan, masks)

        # 12. finishing
        if abs(g.post_crop_vignette) > 0.5:
            toned = stage("vignette", self._vignette, toned, g.post_crop_vignette / 100.0)
        if g.grain > 0.5:
            toned = stage("grain", self._grain, toned, g.grain / 100.0, shape)

        out = stage("safe_highlights", self._safe_highlights, toned)
        out = gamut_compress(out)
        report.ms_stages = stages
        report.ms_total = (time.perf_counter() - t_start) * 1000.0
        report.height, report.width = shape
        report.mechanical = {"exposure_factor": round(float(exposure), 4),
                             "temperature": g.temperature, "tint": g.tint}
        return out, report

    # ---------------------------------------------------------------- stages
    @staticmethod
    def _white_balance(lin: np.ndarray, temp: float, tint: float) -> np.ndarray:
        m = color.wb_matrix(temp, tint).astype(np.float32)
        out = lin @ m.T
        return np.maximum(out, 0.0)

    @staticmethod
    def _correct_vignette(lin: np.ndarray, ratio: float) -> np.ndarray:
        h, w = lin.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        cy, cx = (h - 1) / 2, (w - 1) / 2
        r = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2) / math.sqrt(2.0)
        deficit = clamp(1.0 - ratio, 0.0, 0.5)
        gain = 1.0 + deficit * (r ** 2.4) * 1.25
        return np.clip(lin * gain[..., None], 0, None)

    @staticmethod
    def _correct_lateral_ca(lin: np.ndarray) -> np.ndarray:
        """Estimate per-channel radial scale by minimising colour fringing."""
        h, w = lin.shape[:2]
        scale = min(1.0, 640.0 / max(h, w))
        small = cv2.resize(lin, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        gray = _lum(small)
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        edges = np.hypot(gx, gy) > max(float(np.percentile(np.hypot(gx, gy), 96)), 0.02)
        if edges.sum() < 200:
            return lin
        centre = (small.shape[1] / 2, small.shape[0] / 2)

        def rescaled(ch: int, s: float) -> np.ndarray:
            m = np.float32([[s, 0, centre[0] * (1 - s)], [0, s, centre[1] * (1 - s)]])
            return cv2.warpAffine(small[..., ch], m, (small.shape[1], small.shape[0]),
                                  flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

        gch = small[..., 1]
        best = {}
        for ch in (0, 2):
            best[ch], best_score = 1.0, float("inf")
            for s in np.linspace(0.9985, 1.0015, 13):
                diff = (rescaled(ch, s) - gch)[edges]
                score = float(np.median(np.abs(diff)))
                if score < best_score:
                    best_score, best[ch] = score, float(s)
        if abs(best[0] - 1) < 2e-4 and abs(best[2] - 1) < 2e-4:
            return lin
        out = lin.copy()
        cy, cx = (h - 1) / 2, (w - 1) / 2
        for ch in (0, 2):
            s = best[ch]
            m = np.float32([[s, 0, cx * (1 - s)], [0, s, cy * (1 - s)]])
            out[..., ch] = cv2.warpAffine(lin[..., ch], m, (w, h), flags=cv2.INTER_LINEAR,
                                          borderMode=cv2.BORDER_REFLECT)
        return out

    @staticmethod
    def _tone_curve_display(x: np.ndarray, g: GlobalAdjust) -> np.ndarray:
        """Lightroom-style tone controls in perceptual (display-luminance) space."""
        x = np.clip(x, 0.0, 1.0)
        # highlights: lift/compress the upper range
        up = _smoothstep(x, 0.42, 1.0)
        x = x + (g.highlights / 100.0) * 0.17 * up * (1.0 - x)
        # shadows: open/close low mids
        low = 1.0 - _smoothstep(x, 0.02, 0.68)
        x = x + (g.shadows / 100.0) * 0.30 * low * (1.0 - x)
        # whites / blacks: endpoint control
        x = x + (g.whites / 100.0) * 0.12 * _smoothstep(x, 0.55, 1.0) * (1.0 - x)
        x = x + (g.blacks / 100.0) * 0.14 * (1.0 - _smoothstep(x, 0.0, 0.38)) * x
        x = np.clip(x, 0.0, 1.0)
        # contrast: S-curve around the mid-grey pivot, Lightroom-like strength
        c = float(clamp(g.contrast / 100.0, -1.0, 1.0))
        if c > 0:
            s_curve = x * x * (3 - 2 * x)
            x = x * (1 - c) + s_curve * c
            x = 0.46 + (x - 0.46) * (1 + 0.14 * c)
        elif c < 0:
            flat = 0.5 + (x - 0.5) * 0.80
            x = x * (1 + c) - flat * c
        return np.clip(x, 0.0, 1.0)

    @staticmethod
    def _lab_l(img: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        lab = color.rgb_to_lab(img)
        return lab, lab[..., 0] / 100.0, lab[..., 1:]

    @staticmethod
    def _with_l(img: np.ndarray, lab: np.ndarray, new_l: np.ndarray) -> np.ndarray:
        out = lab.copy()
        out[..., 0] = np.clip(new_l, 0.0, 1.15) * 100.0
        return np.clip(color.lab_to_rgb(out), 0, 1)

    def _to_display(self, lin: np.ndarray) -> np.ndarray:
        """Linear -> display with a soft highlight shoulder for values above white.

        The shoulder is applied as a luminance *gain* (bounded to 1.0) on pixels that
        are already near-white, so hue never shifts while recoverable headroom is
        compressed instead of hard-clipped.
        """
        lum = _lum(lin)
        if float(lum.max()) > 1.0:
            knee, span = 0.94, 0.06 + 0.5
            over = lum > knee
            comp = knee + span * (1.0 - np.exp(-(lum[over] - knee) / span))
            k = np.ones_like(lum)
            k[over] = comp / np.maximum(lum[over], 1e-6)
            lin = lin * k[..., None]
        return color.linear_to_srgb(np.clip(lin, 0.0, 1.0))

    def _tone_map(self, lin: np.ndarray, g: GlobalAdjust) -> np.ndarray:
        disp = self._to_display(lin)
        lab, L, _ = self._lab_l(disp)
        target = self._tone_curve_display(L, g)
        return self._with_l(disp, lab, target)

    def _apply_curves(self, img: np.ndarray, g: GlobalAdjust) -> np.ndarray:
        out = img.copy()
        if g.curve_rgb:
            lut = np.array(g.curve_rgb, np.float32)
            out = np.stack([color.apply_lut(out[..., i], lut) for i in range(3)], axis=2)
        for ch, pts in ((0, g.curve_red), (2, g.curve_blue)):
            if pts:
                lut = np.array(pts, np.float32)
                out[..., ch] = color.apply_lut(out[..., ch], lut)
        return np.clip(out, 0, 1)

    def _dehaze(self, img: np.ndarray, amount: float) -> np.ndarray:
        amount = clamp(amount, -0.5, 1.0)
        h, w = img.shape[:2]
        scale = min(1.0, 700.0 / max(h, w))
        small = cv2.resize(img, (max(8, int(w * scale)), max(8, int(h * scale))),
                           interpolation=cv2.INTER_AREA) if scale < 1.0 else img
        dark = small.min(axis=2)
        k = max(3, int(min(small.shape[:2]) * 0.03) | 1)
        dark = cv2.erode(dark, np.ones((k, k), np.uint8))
        # Atmospheric light from the brightest dark-channel pixels
        flat = dark.reshape(-1)
        idx = np.argsort(flat)[-max(1, int(flat.size * 0.001)):]
        atmos = np.percentile(small.reshape(-1, 3)[idx], 75, axis=0)
        atmos = np.maximum(atmos, 0.35)
        trans = 1.0 - 0.88 * dark / max(float(atmos.max()), 1e-3)
        trans = np.clip(trans, 0.25, 1.0)
        trans = _guided(small, trans, radius=max(8, k * 3), eps=1e-3)
        j = (small - atmos[None, None, :]) / trans[..., None] + atmos[None, None, :]
        j = np.clip(j, 0, 1)
        if amount >= 0:
            res = small * (1 - amount) + j * amount
        else:
            # Negative dehaze should *add* atmosphere. Subtracting the retrieved
            # radiance inverts hue relationships (magenta skies), so instead the
            # image is veiled toward the scene's own airlight colour, which is
            # what haze physically does.
            veil = float(min(0.55, -amount))
            airlight = np.clip(atmos * 0.55 + 0.45, 0.0, 1.0)
            res = small * (1.0 - veil) + airlight[None, None, :] * veil
        if scale < 1.0:
            res = cv2.resize(res, (w, h), interpolation=cv2.INTER_LINEAR)
        # restore luminance so the exposure decisions are not undone by dehaze
        original_l = _lum(img)
        target_ratio = np.clip(original_l / np.maximum(_lum(res), 1e-4), 0.6, 1.6)
        out = res * np.power(target_ratio, 0.65)[..., None]
        return np.clip(out, 0, 1)

    def _denoise(self, img: np.ndarray, g: GlobalAdjust, sigma: float) -> np.ndarray:
        luma_strength = clamp(g.luminance_nr / 100.0, 0.0, 1.0)
        chroma_strength = clamp(g.color_nr / 100.0, 0.0, 1.0)
        detail_keep = clamp(g.luminance_detail / 100.0, 0.0, 1.0)
        lab = color.rgb_to_lab(img)
        L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
        # Chroma noise lives in a/b: smooth with an L-guided filter so edges hold.
        if chroma_strength > 0.02:
            r = max(4, int(min(img.shape[:2]) * 0.012))
            a_s = _guided(L, a, r, 2.0)
            b_s = _guided(L, b, r, 2.0)
            a = a * (1 - chroma_strength) + a_s * chroma_strength
            b = b * (1 - chroma_strength) + b_s * chroma_strength
        # Luma noise: base/detail split; smoothing the base removes noise while
        # leaving the detail layer (which carries texture) largely intact.
        if luma_strength > 0.02:
            r = max(3, int(min(img.shape[:2]) * 0.006))
            base = _guided(L, L, r, max(4.0, 30.0 * luma_strength))
            detail = L - base
            sig = max(sigma, 0.01)
            noise_floor = clamp(sig * 100.0 * 0.55, 0.4, 6.0)
            # shrink detail smoothly near the noise floor instead of thresholding
            dkeep = clamp(1.0 - noise_floor / (np.abs(detail) + noise_floor) * (1 - detail_keep),
                          0.0, 1.0)
            dkeep = 0.25 + 0.75 * dkeep
            L_new = base + detail * dkeep * (1 - 0.35 * luma_strength) + detail * 0.35 * luma_strength * detail_keep
            L = L * (1 - luma_strength) + L_new * luma_strength
        out = color.lab_to_rgb(np.stack([L, a, b], axis=-1))
        return np.clip(out, 0, 1)

    def _local_contrast(self, img: np.ndarray, texture: float, clarity: float) -> np.ndarray:
        lab, L, _ = self._lab_l(img)
        delta = np.zeros_like(L)
        if abs(clarity) > 0.5:
            r = max(8, int(min(img.shape[:2]) * 0.035))
            base = _guided(L, L, r, 0.012)
            detail = np.clip(L - base, -0.13, 0.13)     # halo guard
            delta += (clarity / 100.0) * 1.15 * detail
        if abs(texture) > 0.5:
            r = max(2, int(min(img.shape[:2]) * 0.006))
            base = _guided(L, L, r, 0.003)
            detail = np.clip(L - base, -0.12, 0.12)
            delta += (texture / 100.0) * 0.85 * detail
        # midtone weighting: no crunchy skies, no muddy shadows
        w = color.smoothstep(L, 0.02, 0.10) * (1.0 - color.smoothstep(L, 0.94, 1.0))
        return self._with_l(img, lab, L + delta * w)

    def _colour(self, img: np.ndarray, g: GlobalAdjust, skin_protection: float) -> np.ndarray:
        # --- 1. perceptual vibrance / saturation in Lab, skin-protected ---------
        if abs(g.vibrance) > 0.5 or abs(g.saturation) > 0.5:
            lab = color.rgb_to_lab(img)
            L, aa, bb = lab[..., 0], lab[..., 1], lab[..., 2]
            C = np.sqrt(aa * aa + bb * bb)
            hue = np.degrees(np.arctan2(bb, aa)) % 360
            # Skin sits near 50–60° in CIELAB hue for most complexions.
            hue_dist = np.abs(((hue - 55.0 + 180) % 360) - 180)
            skin_aff = np.exp(-((hue_dist / 24.0) ** 2)) * np.exp(-(((C - 30) / 26.0) ** 2))
            protect = float(np.clip(skin_protection, 0, 1)) * np.clip(skin_aff, 0, 1) * 0.85
            if abs(g.vibrance) > 0.5:
                weight = np.clip(1.0 - C / 70.0, 0.0, 1.0) * (1.0 - protect)
                C = C * (1.0 + (g.vibrance / 100.0) * 1.05 * weight)
            if abs(g.saturation) > 0.5:
                C = C * (1.0 + (g.saturation / 100.0) * 0.75 * (1.0 - protect * 0.9))
            new_lab = np.stack([L, C * np.cos(np.radians(hue)), C * np.sin(np.radians(hue))], axis=-1)
            out = color.lab_to_rgb(new_lab)
            img = np.clip(out, 0, 1)

        # --- 2. targeted HSL in HSV, matching Lightroom's channel semantics -----
        any_hsl = any(abs(v) > 0.5 for v in list(g.hsl_hue.values()) +
                      list(g.hsl_saturation.values()) + list(g.hsl_luminance.values()))
        if not any_hsl:
            return img
        hsv = cv2.cvtColor(np.clip(img, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
        H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        dH = np.zeros_like(H)
        sMul = np.ones_like(S)
        lMul = np.ones_like(V)
        channels = [("red", 0.0), ("orange", 30.0), ("yellow", 60.0), ("green", 120.0),
                    ("aqua", 180.0), ("blue", 240.0), ("purple", 285.0), ("magenta", 325.0)]
        for name, centre in channels:
            dh = float(g.hsl_hue.get(name, 0.0))
            ds = float(g.hsl_saturation.get(name, 0.0))
            dl = float(g.hsl_luminance.get(name, 0.0))
            if abs(dh) < 0.5 and abs(ds) < 0.5 and abs(dl) < 0.5:
                continue
            dist = np.abs(((H - centre + 180) % 360) - 180)
            w = np.clip(1.0 - dist / 45.0, 0.0, 1.0)
            w = w * w * (3 - 2 * w)
            if abs(dh) > 0.5:
                dH += (dh / 100.0) * 30.0 * w
            if abs(ds) > 0.5:
                sMul *= 1.0 + (ds / 100.0) * 0.85 * w
            if abs(dl) > 0.5:
                lMul *= 1.0 + (dl / 100.0) * 0.35 * w
        H = (H + dH) % 360.0
        S = np.clip(S * sMul, 0, 1)
        V = np.clip(V * lMul, 0, 1)
        out = cv2.cvtColor(np.stack([H, S, V], axis=-1), cv2.COLOR_HSV2RGB)
        return np.clip(out, 0, 1)

    # ---------------------------------------------------------------- masks
    def _mask_array(self, layer: MaskLayer, masks: Dict[str, np.ndarray],
                    shape: Tuple[int, int]) -> Optional[np.ndarray]:
        cache_key = self._mask_cache_key(layer, shape)
        if cache_key in self._mask_cache:
            cached = self._mask_cache[cache_key]
            return None if cached is None else cached
        built = self._build_mask_array(layer, masks, shape)
        if len(self._mask_cache) < 64:
            self._mask_cache[cache_key] = built
        return built

    def _mask_cache_key(self, layer: MaskLayer, shape: Tuple[int, int]) -> str:
        geometry = layer.geometry or {}
        faces = geometry.get("faces") or []
        sig = [
            layer.kind, bool(layer.inverted), round(float(layer.feather), 3),
            round(float(layer.opacity), 3), int(shape[0]), int(shape[1]),
            str(geometry.get("mask_kind") or ""),
            [[round(float(f.get(k, 0.0)), 4) for k in ("x", "y", "w", "h")] for f in faces],
            {k: v for k, v in geometry.items() if k != "faces"} if layer.kind in
            ("radial", "linear") else "",
        ]
        return json.dumps(sig, sort_keys=True, default=str)

    def _build_mask_array(self, layer: MaskLayer, masks: Dict[str, np.ndarray],
                          shape: Tuple[int, int]) -> Optional[np.ndarray]:
        h, w = shape
        kind = layer.kind
        base: Optional[np.ndarray] = None
        if kind == "eyes":
            base = self._eye_mask(layer, shape)
        elif kind == "highlights":
            # Rendered from the current pixels by the caller (needs the image).
            base = None
        elif layer.geometry.get("mask_kind"):
            base = masks.get(str(layer.geometry["mask_kind"]))
        else:
            base = masks.get(kind)
        if base is None and kind in ("radial", "linear"):
            base = self._geometric_mask(layer, shape)
        if base is None:
            return None
        m = _resize_to(base.astype(np.float32), shape)
        if layer.inverted:
            m = 1.0 - m
        if layer.feather > 0:
            r = max(1, int(min(h, w) * 0.004 * (1 + layer.feather * 8)))
            m = _guided(m, m, r, 2e-3)
        m = np.clip(m * float(layer.opacity), 0, 1)
        return m

    @staticmethod
    def _eye_mask(layer: MaskLayer, shape: Tuple[int, int]) -> np.ndarray:
        h, w = shape
        m = np.zeros((h, w), np.float32)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        for f in layer.geometry.get("faces", []) or []:
            fx, fy, fw, fh = [float(v) for v in (f.get("x", 0), f.get("y", 0),
                                                 f.get("w", 0), f.get("h", 0))]
            # eye band occupies 20–48% of face height, 8–92% width
            cx0, cx1 = (fx + fw * 0.08) * w, (fx + fw * 0.92) * w
            cy0, cy1 = (fy + fh * 0.20) * h, (fy + fh * 0.48) * h
            inside = ((xx > cx0) & (xx < cx1) & (yy > cy0) & (yy < cy1)).astype(np.float32)
            feather = max(2.0, fw * w * 0.03)
            m = np.maximum(m, cv2.GaussianBlur(inside, (0, 0), feather))
        return np.clip(m, 0, 1)

    @staticmethod
    def _geometric_mask(layer: MaskLayer, shape: Tuple[int, int]) -> np.ndarray:
        h, w = shape
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        geom = layer.geometry
        if layer.kind == "radial":
            cx, cy = float(geom.get("cx", 0.5)) * w, float(geom.get("cy", 0.5)) * h
            rx = max(float(geom.get("rx", 0.25)) * w, 2)
            ry = max(float(geom.get("ry", 0.25)) * h, 2)
            d = np.sqrt(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2)
            return np.clip(1.0 - color.smoothstep(d, 0.75, 1.15), 0, 1).astype(np.float32)
        # linear gradient
        x0, y0 = float(geom.get("x0", 0.5)) * w, float(geom.get("y0", 0.2)) * h
        x1, y1 = float(geom.get("x1", 0.5)) * w, float(geom.get("y1", 0.8)) * h
        vx, vy = x1 - x0, y1 - y0
        norm = max(math.hypot(vx, vy), 1.0)
        t = ((xx - x0) * vx + (yy - y0) * vy) / (norm * norm)
        return np.clip(t, 0, 1).astype(np.float32)

    def _apply_adjustment(self, img: np.ndarray, adj: GlobalAdjust,
                          strength: np.ndarray) -> np.ndarray:
        """Apply a masked adjustment inside the mask's bounding box, then blend.

        Every operation here is spatially local (per-pixel tone/colour, or a
        kernel-bounded blur), so restricting the work to the masked region gives
        identical pixels for a fraction of the cost. The box is grown by a small
        margin so neighbourhood operators see real pixels at the boundary.
        """
        strength = np.clip(strength, 0, 1)
        ys, xs = np.nonzero(strength > 0.004)
        if ys.size == 0:
            return img
        h, w = img.shape[:2]
        pad = 12
        y0, y1 = max(0, int(ys.min()) - pad), min(h, int(ys.max()) + 1 + pad)
        x0, x1 = max(0, int(xs.min()) - pad), min(w, int(xs.max()) + 1 + pad)
        sub = img[y0:y1, x0:x1]
        if (y1 - y0) * (x1 - x0) >= 0.85 * h * w:
            processed = self._apply_adjustment_full(img, adj)
            blend = strength[..., None]
            return np.clip(img * (1 - blend) + processed * blend, 0, 1)
        processed = self._apply_adjustment_full(sub, adj)
        blend = strength[y0:y1, x0:x1, None]
        out = img.copy()
        out[y0:y1, x0:x1] = np.clip(sub * (1 - blend) + processed * blend, 0, 1)
        return out

    def _apply_adjustment_full(self, img: np.ndarray, adj: GlobalAdjust) -> np.ndarray:
        """The unblended adjustment itself (no mask composite)."""
        out = img
        if adj.temperature is not None:
            lin = color.srgb_to_linear(out)
            lin = self._white_balance(lin, float(adj.temperature),
                                      float(adj.tint if adj.tint is not None else 0.0))
            out = color.linear_to_srgb(np.clip(lin, 0, 1))
        if abs(adj.exposure) > 0.001:
            lin = color.srgb_to_linear(out) * (2.0 ** float(adj.exposure))
            out = color.linear_to_srgb(np.clip(lin, 0, 1))
        tone = GlobalAdjust(
            contrast=adj.contrast, highlights=adj.highlights, shadows=adj.shadows,
            whites=adj.whites, blacks=adj.blacks)
        if any(abs(v) > 0.5 for v in (tone.contrast, tone.highlights, tone.shadows,
                                      tone.whites, tone.blacks)):
            out = self._apply_tone_only(out, tone)
        if abs(adj.texture) > 0.5 or abs(adj.clarity) > 0.5:
            out = self._local_contrast(out, adj.texture, adj.clarity)
        if abs(adj.saturation) > 0.5 or abs(adj.vibrance) > 0.5 or any(
                abs(v) > 0.5 for v in list(adj.hsl_hue.values()) +
                list(adj.hsl_saturation.values()) + list(adj.hsl_luminance.values())):
            out = self._colour(out, adj, 0.9 if adj is not None else 0.0)
        return np.clip(out, 0, 1)

    def _apply_tone_only(self, img: np.ndarray, g: GlobalAdjust) -> np.ndarray:
        lab, L, _ = self._lab_l(img)
        return self._with_l(img, lab, self._tone_curve_display(L, g))

    def _apply_masks(self, img: np.ndarray, plan: EditPlan,
                     masks: Dict[str, np.ndarray]) -> np.ndarray:
        out = img
        for layer in plan.masks:
            if not layer.enabled:
                continue
            if layer.adjustments.is_noop() and abs(layer.adjustments.sharpness - 40) < 1:
                continue
            m = self._mask_array(layer, masks, img.shape[:2])
            if m is None or float(m.max()) < 0.01:
                continue
            out = self._apply_adjustment(out, layer.adjustments, m)
        return out

    # ---------------------------------------------------------------- detail
    @staticmethod
    def _edge_mask(L: np.ndarray, masking: float) -> np.ndarray:
        gx = cv2.Sobel(L, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(L, cv2.CV_32F, 0, 1, ksize=3)
        mag = np.hypot(gx, gy)
        scale = max(float(np.percentile(mag, 99)), 1e-4)
        n = mag / scale
        thr = 0.02 + (masking / 100.0) * 0.35
        return np.clip((n - thr) / max(0.25 - thr * 0.5, 0.05), 0, 1)

    def _sharpen(self, img: np.ndarray, plan: EditPlan,
                 masks: Dict[str, np.ndarray]) -> np.ndarray:
        g = plan.global_
        amount = clamp(g.sharpness / 100.0, 0, 1.5)
        radius = clamp(g.sharpen_radius, 0.5, 3.0)
        detail_amt = clamp(g.sharpen_detail / 100.0, 0, 1)
        lab, L, _ = self._lab_l(img)
        blur1 = cv2.GaussianBlur(L, (0, 0), max(0.4, radius * 0.55))
        blur2 = cv2.GaussianBlur(L, (0, 0), max(0.8, radius * 1.6))
        d1 = L - blur1
        d2 = L - blur2
        detail = (0.65 + 0.35 * detail_amt) * d1 + (0.35 - 0.15 * detail_amt) * d2
        edge = self._edge_mask(L, g.sharpen_masking)
        noise_sigma = float(plan.detail.get("noise_sigma", 0.01)) * 1.0
        gate = np.clip((np.abs(detail) - noise_sigma * 2.0) / (noise_sigma * 2.0 + 1e-4), 0, 1)
        gain = amount * edge * (0.30 + 0.70 * gate)

        for layer in plan.masks:
            if not layer.enabled:
                continue
            delta = float(layer.adjustments.sharpness) - 40.0
            if abs(delta) < 1.0:
                continue
            m = self._mask_array(layer, masks, img.shape[:2])
            if m is None:
                continue
            gain = gain + (delta / 100.0) * m

        newL = L + detail * gain
        # halo limiter: bound local overshoot to a few percent around neighbours
        k = 3
        lo = cv2.erode(L, np.ones((k, k), np.uint8))
        hi = cv2.dilate(L, np.ones((k, k), np.uint8))
        newL = np.clip(newL, lo - 0.012, hi + 0.012)
        return self._with_l(img, lab, newL)

    def _deconvolve(self, img: np.ndarray, plan: EditPlan) -> np.ndarray:
        """Richardson–Lucy detail recovery for slightly soft frames.

        A Gaussian PSF is a deliberately honest approximation: without a real PSF
        measurement this cannot be called true deconvolution, so it is only used at
        Maximum quality, blended and noise-gated.
        """
        lab, L, _ = self._lab_l(img)
        sigma = clamp(float(plan.sharpening.get("psf_sigma", 1.0)), 0.6, 1.8)
        iters = int(clamp(float(plan.sharpening.get("iterations", 6)), 3, 10))
        eps = 1e-4
        obs = np.clip(L, eps, 1)
        est = obs.copy()
        psf = max(1.0, sigma)
        for _ in range(iters):
            conv = cv2.GaussianBlur(est, (0, 0), psf)
            ratio = obs / np.maximum(conv, eps)
            est = est * cv2.GaussianBlur(ratio, (0, 0), psf)
            est = np.clip(est, 0, 1.5)
        strength = clamp(float(plan.detail.get("deconvolution", 0.35)), 0, 0.8)
        newL = L * (1 - strength) + est * strength
        return self._with_l(img, lab, newL)

    @staticmethod
    def _safe_highlights(img: np.ndarray, target_clip: float = 0.006) -> np.ndarray:
        """Final print-safe highlight guard.

        Masks and local contrast can push a few bright pixels past white.  Rather
        than let them clip to flat patches, compress the extreme top end softly.
        Bounded by design: it only engages when clipping exceeds the target.
        """
        lum = _lum(np.clip(img, 0, 1))
        clip = float((lum > 0.995).mean())
        if clip <= target_clip:
            return img
        knee, span = 0.90, 0.12
        over = lum > knee
        comp = knee + span * (1.0 - np.exp(-(lum[over] - knee) / span))
        k = np.ones_like(lum)
        k[over] = comp / np.maximum(lum[over], 1e-5)
        return np.clip(img * k[..., None], 0, 1)

    @staticmethod
    def _vignette(img: np.ndarray, amount: float) -> np.ndarray:
        h, w = img.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        cx, cy = (w - 1) / 2, (h - 1) / 2
        r = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2) / math.sqrt(2)
        falloff = np.clip(r ** 2.2, 0, 1)
        gain = 1.0 + amount * (falloff - 0.35)
        return np.clip(img * gain[..., None], 0, 1)

    @staticmethod
    def _grain(img: np.ndarray, amount: float, shape: Tuple[int, int]) -> np.ndarray:
        rng = np.random.default_rng(7)   # deterministic: same plan -> same render
        noise = rng.normal(0, 0.018 * amount, (shape[0], shape[1], 1)).astype(np.float32)
        lum = _lum(img)[..., None]
        # grain is strongest in midtones, like film
        weight = 1.0 - np.abs(lum - 0.5) * 1.4
        return np.clip(img + noise * weight, 0, 1)
