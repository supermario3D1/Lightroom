"""Colour science used across Kestrel.

Design rules
------------
* All rendering maths happens in linear light, in the working space declared by
  the image (sRGB primaries assumed for display data; RAW decoding supplies
  camera-native linear data which is converted to sRGB primaries up front).
* Lightroom-facing numbers are expressed in Lightroom's own slider units so that a
  generated edit can be written back as *native, editable develop settings*.
* Nothing here is a "look" LUT — every transform is parameterised and explainable.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------- primaries
SRGB_TO_XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                        [0.2126729, 0.7151522, 0.0721750],
                        [0.0193339, 0.1191920, 0.9503041]], dtype=np.float64)
XYZ_TO_SRGB = np.linalg.inv(SRGB_TO_XYZ)
_S2X32 = SRGB_TO_XYZ.astype(np.float32)
_X2S32 = XYZ_TO_SRGB.astype(np.float32)
D65_XY = (0.3127, 0.3290)
D65_WHITE = np.array([0.95047, 1.0, 1.08883])

BRADFORD = np.array([[0.8951, 0.2664, -0.1614],
                     [-0.7502, 1.7135, 0.0367],
                     [0.0389, -0.0685, 1.0296]], dtype=np.float64)
BRADFORD_INV = np.linalg.inv(BRADFORD)

# Rec.709 luma weights (identical to sRGB primaries)
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)


# ---------------------------------------------------------------------------- transfer fn
def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    """Display-encoded sRGB [0,1] -> linear light (exact sRGB piecewise curve)."""
    arr = np.clip(np.asarray(x, dtype=np.float32), 0.0, 1.0)
    return np.where(arr <= 0.04045, arr / 12.92,
                    ((arr + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    """Linear light [0,1] -> display-encoded sRGB (exact sRGB piecewise curve)."""
    arr = np.clip(np.asarray(x, dtype=np.float32), 0.0, 1.0)
    return np.where(arr <= 0.0031308, arr * 12.92,
                    1.055 * np.power(arr, 1 / 2.4) - 0.055).astype(np.float32)


def luma_linear(rgb: np.ndarray) -> np.ndarray:
    return (rgb[..., 0] * LUMA[0] + rgb[..., 1] * LUMA[1] + rgb[..., 2] * LUMA[2])


def luma_display(rgb: np.ndarray) -> np.ndarray:
    return (rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114)


# ---------------------------------------------------------------------------- CIELAB
def _f(t: np.ndarray) -> np.ndarray:
    d = 6 / 29
    return np.where(t > d ** 3, np.cbrt(t), t / (3 * d * d) + 4 / 29).astype(np.float32)


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Display-encoded sRGB [0,1] -> CIELAB (D65), float32."""
    lin = srgb_to_linear(rgb)
    xyz = lin.astype(np.float32) @ _S2X32.T
    f = _f(xyz / _W32_arr())
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def _W32_arr() -> np.ndarray:
    return np.array([0.95047, 1.0, 1.08883], dtype=np.float32)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    lab = np.asarray(lab, dtype=np.float32)
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16) / 116
    fx = fy + a / 500
    fz = fy - b / 200
    xyz = np.stack([fx, fy, fz], axis=-1).astype(np.float32)
    d = 6 / 29
    xyz = np.where(xyz > d, xyz ** 3, 3 * d * d * (xyz - 4 / 29)) * _W32_arr()
    lin = xyz @ _X2S32.T
    return linear_to_srgb(lin)


def delta_e76(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    return np.sqrt(((lab1 - lab2) ** 2).sum(axis=-1))


def chroma(lab: np.ndarray) -> np.ndarray:
    return np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)


def hue_angle(lab: np.ndarray) -> np.ndarray:
    return np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) % 360


# ---------------------------------------------------------------------------- white points
def kelvin_to_xy(kelvin: float) -> Tuple[float, float]:
    """Planckian locus approximation (Kim et al. 2002), valid 1667K–25000K."""
    t = float(np.clip(kelvin, 1667, 25000))
    if t <= 4000:
        x = (-0.2661239e9 / t ** 3 - 0.2343589e6 / t ** 2
             + 0.8776956e3 / t + 0.179910)
    else:
        x = (-3.0258469e9 / t ** 3 + 2.1070379e6 / t ** 2
             + 0.2226347e3 / t + 0.240390)
    if x < 0.4:
        # piecewise ranges use the *x* value, not the CCT
        pass
    if t <= 2222:
        y = (-1.1063814 * x ** 3 - 1.34811020 * x ** 2
             + 2.18555832 * x - 0.20219683)
    elif t <= 4000:
        y = (-0.9549476 * x ** 3 - 1.37418593 * x ** 2
             + 2.09137015 * x - 0.16748867)
    else:
        y = (3.0817580 * x ** 3 - 5.87338670 * x ** 2
             + 3.75112997 * x - 0.37001483)
    return float(x), float(y)


def xy_to_uv(x: float, y: float) -> Tuple[float, float]:
    d = (-2 * x + 12 * y + 3)
    return (4 * x / d, 6 * y / d)


def xy_to_cct_tint(x: float, y: float) -> Tuple[float, float]:
    """Approximate CCT (McCamy) and Duv tint for a white point in xy."""
    y = max(float(y), 1e-4)
    n = (x - 0.3320) / (0.1858 - y)
    cct = 449 * n ** 3 + 3525 * n ** 2 + 6823.3 * n + 5520.33
    cct = float(np.clip(cct, 1667, 25000))
    # Duv: signed distance from the Planckian locus in CIE 1960 UCS at that CCT.
    u, v = xy_to_uv(x, y)
    ku, kv = xy_to_uv(*kelvin_to_xy(cct))
    duv = (v - kv) * 1.0
    return cct, float(duv)


def adapt_matrix(src_xy: Tuple[float, float], dst_xy: Tuple[float, float]) -> np.ndarray:
    """Bradford chromatic adaptation between two white points (in XYZ)."""
    def xyz_of(xy):
        x, y = xy
        y = max(y, 1e-5)
        return np.array([x / y, 1.0, (1 - x - y) / y])
    s, d = xyz_of(src_xy), xyz_of(dst_xy)
    cs = BRADFORD @ s
    cd = BRADFORD @ d
    scale = np.diag(cd / cs)
    return BRADFORD_INV @ scale @ BRADFORD


def wb_matrix(temperature: float, tint: float = 0.0) -> np.ndarray:
    """Linear-sRGB white balance matrix that mimics Lightroom's Temperature/Tint.

    ``temperature`` is the assumed illuminant CCT. Higher = warmer light assumed =
    cooler rendering (same convention as Lightroom's slider). ``tint`` runs -150
    (green) to +150 (magenta).
    """
    xy = kelvin_to_xy(temperature)
    m = adapt_matrix(xy, D65_XY)
    gain = 2.0 ** (-float(tint) / 400.0)  # +tint boosts magenta, i.e. reduces green
    g = np.diag([1.0, gain, 1.0])
    # convert XYZ adaptation to linear sRGB and fold in the tint gain
    m_rgb = SRGB_TO_XYZ.T @ m @ XYZ_TO_SRGB.T
    return g @ m_rgb


def apply_wb(rgb_linear: np.ndarray, temperature: float, tint: float = 0.0,
             exposure: float = 1.0) -> np.ndarray:
    m = wb_matrix(temperature, tint) * exposure
    out = rgb_linear @ m.T.astype(np.float32)
    return np.maximum(out, 0.0)


# ---------------------------------------------------------------------------- illuminant
def estimate_illuminant(rgb_display: np.ndarray, mask: np.ndarray | None = None,
                        p: float = 6.0, max_pixels: int = 60_000
                        ) -> Tuple[np.ndarray, float]:
    """Robust grey-edge illuminant estimate.

    Returns ``(scene_rgb, confidence)`` where ``scene_rgb`` is the *estimated colour
    of the light*, normalised to green = 1. A value below 1 in a channel means that
    channel is deficient in the scene light, so the neutralising correction is
    ``1 / scene_rgb``. Uses a Minkowski norm (Shades-of-Grey) on unclipped,
    low-saturation pixels, which is markedly more stable than a plain average on
    sports and stage lighting.
    """
    h, w = rgb_display.shape[:2]
    step = max(1, int(math.sqrt((h * w) / max_pixels)))
    px = rgb_display[::step, ::step].reshape(-1, 3).astype(np.float32)
    if mask is not None:
        m = mask[::step, ::step].reshape(-1).astype(bool)
        if m.sum() > 200:
            px = px[m]
    if px.size == 0:
        return np.ones(3, np.float32), 0.0
    mx = px.max(axis=1)
    mn = px.min(axis=1)
    sat = np.where(mx > 1e-5, (mx - mn) / np.maximum(mx, 1e-5), 0)
    keep = (mx < 0.985) & (mx > 0.04) & (sat < 0.55)
    if keep.sum() < 200:
        keep = (mx < 0.985) & (mx > 0.02)
    sample = srgb_to_linear(px[keep])
    if sample.shape[0] < 50:
        return np.ones(3, np.float32), 0.0
    norm = np.power((sample ** p).mean(axis=0) + 1e-8, 1.0 / p)
    gains = norm / max(norm[1], 1e-6)
    # Confidence: agreement between grey-world and grey-edge estimates.
    mean_g = sample.mean(axis=0) / max(sample.mean(axis=0)[1], 1e-6)
    spread = float(np.abs(gains - mean_g).max())
    conf = float(np.clip(1.0 - spread * 2.2, 0.0, 1.0))
    return gains.astype(np.float32), conf


def neutral_samples(rgb_display: np.ndarray, exclude: np.ndarray | None = None,
                    min_luma: float = 0.35, max_luma: float = 0.97,
                    max_sat: float = 0.20, limit: int = 40_000) -> np.ndarray:
    """Linear-light samples of likely neutral surfaces (white kit, paint, paper).

    Highlights are preferred: a white reference under a coloured illuminant is the
    single most reliable WB cue in sport and event photography.
    """
    h, w = rgb_display.shape[:2]
    step = max(1, int(math.sqrt((h * w) / 120_000)))
    px = rgb_display[::step, ::step].reshape(-1, 3).astype(np.float32)
    mx = px.max(axis=1)
    mn = px.min(axis=1)
    sat = np.where(mx > 1e-5, (mx - mn) / np.maximum(mx, 1e-5), 0)
    lum = px @ LUMA.astype(np.float32)
    keep = (sat < max_sat) & (lum > min_luma) & (lum < max_luma) & (mx > 0.05)
    if exclude is not None:
        ex = exclude[::step, ::step].reshape(-1).astype(bool)
        keep &= ~ex
    if keep.sum() < 40:
        return np.zeros((0, 3), np.float32)
    sel = px[keep]
    # Prefer the brightest 60% of the neutral candidates (white-patch logic).
    lum_sel = sel @ LUMA.astype(np.float32)
    thr = np.percentile(lum_sel, 40)
    sel = sel[lum_sel >= thr]
    if sel.shape[0] > limit:
        idx = np.linspace(0, sel.shape[0] - 1, limit).astype(int)
        sel = sel[idx]
    return srgb_to_linear(sel)


def solve_wb(rgb_display: np.ndarray | None = None,
             samples_linear: np.ndarray | None = None,
             exclude: np.ndarray | None = None) -> Tuple[float, float, float, float]:
    """Find the Lightroom Temperature/Tint that neutralises the measured cast.

    Returns ``(temperature_K, tint, cast_strength, confidence)``.

    Implemented as a coarse-to-fine search over the WB matrix, scoring residual
    chromaticity on near-neutral samples.  This is deliberately empirical:
    Lightroom's own camera profiles differ per body, so the result is a
    *starting point* the renderer refines and the plugin writes as editable values.
    """
    samples = samples_linear
    if samples is None or samples.shape[0] < 20:
        if rgb_display is None:
            return 6500.0, 0.0, 0.0, 0.0
        samples = neutral_samples(rgb_display, exclude=exclude)
    if samples is None or samples.shape[0] < 20:
        return 6500.0, 0.0, 0.0, 0.0
    if samples.shape[0] > 20_000:
        idx = np.linspace(0, samples.shape[0] - 1, 20_000).astype(int)
        samples = samples[idx]
    samples = np.maximum(samples.astype(np.float32), 1e-5)
    mean_s = samples.mean(axis=0) + 1e-6
    gains = mean_s / max(mean_s[1], 1e-6)
    cast = float(np.abs(np.log2(np.maximum(gains, 1e-4))).mean())
    # Consistency of candidate chromaticities → WB confidence.
    norm = samples / np.maximum(samples @ LUMA.astype(np.float32)[:, None], 1e-6)
    spread = float(np.median(np.abs(norm - np.median(norm, axis=0)).sum(axis=1)))
    conf = float(np.clip((0.35 - spread) / 0.30, 0.0, 1.0) * np.clip(samples.shape[0] / 4000.0, 0.15, 1.0))
    # The camera's response to the white reference *is* the illuminant estimate.
    white = (mean_s / max(mean_s[1], 1e-6)).astype(np.float32)
    xyz = white @ SRGB_TO_XYZ.T
    tot = float(xyz.sum())
    x, y = float(xyz[0] / max(tot, 1e-6)), float(xyz[1] / max(tot, 1e-6))
    cct, duv = xy_to_cct_tint(x, y)
    t_seed = float(np.clip(cct, 2000.0, 12000.0))
    tint_seed = float(np.clip(duv * 3800.0, -80.0, 80.0))

    def score(temp: float, tint: float) -> float:
        m = wb_matrix(temp, tint).astype(np.float32)
        out = np.maximum(m @ white, 1e-6)
        out = out / max(float(out.mean()), 1e-6)
        residual = float(np.mean(np.abs(np.log(out))))
        prior = 0.008 * abs(math.log2(temp / 6500.0)) + 0.0006 * abs(tint)
        return residual + prior

    best = (score(t_seed, tint_seed), t_seed, tint_seed)
    for temp in np.arange(max(1800.0, t_seed - 700), t_seed + 701, 50):
        for tint in np.arange(tint_seed - 18, tint_seed + 18.1, 3):
            tt = float(np.clip(tint, -150, 150))
            sc = score(float(temp), tt)
            if sc < best[0]:
                best = (sc, float(temp), tt)
    return float(best[1]), float(best[2]), cast, conf


# ---------------------------------------------------------------------------- tone helpers
def percentile(values: np.ndarray, q: float) -> float:
    if values.size == 0:
        return 0.0
    return float(np.percentile(values, q))


def smoothstep(x: np.ndarray, edge0: float, edge1: float) -> np.ndarray:
    if edge1 == edge0:
        return (x >= edge1).astype(np.float32)
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return (t * t * (3 - 2 * t)).astype(np.float32)


def curve_lut(anchors: Sequence[Tuple[float, float]], size: int = 256,
              monotone: bool = True) -> np.ndarray:
    """Build a 0..1 LUT from (x, y) anchors with monotone cubic interpolation."""
    xs = np.array([a[0] for a in anchors], dtype=np.float64)
    ys = np.array([a[1] for a in anchors], dtype=np.float64)
    xs, order = np.unique(np.clip(xs, 0, 1), return_index=True)
    ys = ys[order]
    if xs.size == 1:
        return np.full(size, float(ys[0]), np.float32)
    x = np.linspace(0, 1, size)
    try:
        from scipy.interpolate import PchipInterpolator
        lut = PchipInterpolator(xs, ys)(x)
    except Exception:  # pragma: no cover - scipy always present
        lut = np.interp(x, xs, ys)
    lut = np.clip(lut, 0, 1)
    if monotone:
        lut = np.maximum.accumulate(lut)
    return lut.astype(np.float32)


def apply_lut(x: np.ndarray, lut: np.ndarray) -> np.ndarray:
    pos = np.clip(x, 0, 1) * (len(lut) - 1)
    i0 = np.floor(pos).astype(np.int32)
    i1 = np.minimum(i0 + 1, len(lut) - 1)
    f = pos - i0
    return lut[i0] * (1 - f) + lut[i1] * f


def lr_tone_curve_points(lut: np.ndarray, count: int = 25) -> List[int]:
    """Sample a 0..1 LUT into Lightroom's 0..255 curve point list."""
    xs = np.linspace(0, 1, count)
    ys = apply_lut(xs.astype(np.float32), lut)
    return [int(round(v)) for v in np.concatenate([xs, ys]) * 255]


# ---------------------------------------------------------------------------- skin
# Classic YCbCr skin locus, tightened for Caucasian through deep skin tones.
SKIN_CB = (105.0, 18.0)
SKIN_CR = (152.0, 16.0)


def rgb_to_ycbcr(rgb: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128 - 0.168736 * r * 255 - 0.331264 * g * 255 + 0.5 * b * 255
    cr = 128 + 0.5 * r * 255 - 0.418688 * g * 255 - 0.081312 * b * 255
    return y, cb, cr


def skin_prior(rgb_display: np.ndarray) -> np.ndarray:
    """Soft 0..1 skin probability from colour alone (used to refine face masks)."""
    y, cb, cr = rgb_to_ycbcr(rgb_display)
    p = np.exp(-(((cb - SKIN_CB[0]) / SKIN_CB[1]) ** 2 +
                 ((cr - SKIN_CR[0]) / SKIN_CR[1]) ** 2) / 2.0)
    p = p.astype(np.float32)
    p *= smoothstep(y, 0.06, 0.16) * (1.0 - smoothstep(y, 0.965, 0.995))
    return p


def guide_weight(rgb_display: np.ndarray, sigma: float = 0.08) -> np.ndarray:
    """Colour distance used to discourage bleeding across hard edges."""
    lab = rgb_to_lab(rgb_display)
    return lab
