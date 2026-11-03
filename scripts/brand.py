#!/usr/bin/env python3
"""Generate every Kestrel brand asset from one geometric definition.

No external rasteriser is required: the master shapes are drawn with Pillow at
4× and downsampled with Lanczos, and the SVG files are written from the same
coordinates so the vector and bitmap marks cannot drift apart.

Outputs
    assets/brand/kestrel-mark.svg          512×512 tile, scalable master
    assets/brand/kestrel-logo.svg          mark + wordmark lockup
    assets/brand/kestrel-mark.png          1024×1024 transparent-friendly mark
    assets/brand/kestrel-icon-*.png        32, 64, 128, 256, 512, 1024
    assets/brand/kestrel-logo-dark.png     lockup for dark backgrounds
    assets/brand/kestrel-logo-light.png    lockup for light backgrounds
    assets/brand/social-preview.png        1280×640 Open Graph card
    web/brand-mark.svg, web/favicon.svg, web/social-preview.png

Usage
    python scripts/brand.py
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import warnings
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets" / "brand"
WEB = ROOT / "web"

SS = 4                     # supersampling factor
SIZE = 512                 # logical mark size
INK = "rgb"                # marker only

NAVY_TOP = (13, 20, 35)
NAVY_BOTTOM = (28, 42, 68)
GOLD_DARK = (224, 138, 30)
GOLD_LIGHT = (255, 198, 92)
PAPER = (248, 250, 252)
SLATE = (154, 168, 190)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _font_candidates(bold: bool) -> list[Path]:
    """Where the wordmark's typeface may live, in order of preference.

    ``KESTREL_BRAND_FONT_BOLD`` / ``KESTREL_BRAND_FONT_REGULAR`` override
    everything, which is how a designer drops in a licensed face. After that we
    look for DejaVu (the face the shipped assets were drawn with) on Linux,
    macOS and Windows, and finally fall back to a system sans.
    """
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates: list[Path] = []
    override = os.environ.get("KESTREL_BRAND_FONT_BOLD" if bold else
                              "KESTREL_BRAND_FONT_REGULAR")
    if override:
        candidates.append(Path(override).expanduser())
    candidates += [
        ROOT / "assets" / "fonts" / name,
        Path("/usr/share/fonts/truetype/dejavu") / name,
        Path("/usr/share/fonts/dejavu") / name,
        Path("/usr/local/share/fonts") / name,
        Path.home() / ".fonts" / name,
        Path.home() / "Library" / "Fonts" / name,
        Path("/Library/Fonts") / name,
        Path("/System/Library/Fonts/Supplemental") / ("Arial Bold.ttf" if bold else "Arial.ttf"),
        Path("C:/Windows/Fonts") / ("arialbd.ttf" if bold else "arial.ttf"),
    ]
    return candidates


def _font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    """Load the wordmark face at ``size`` px, cross-platform."""
    for candidate in _font_candidates(bold):
        try:
            if candidate.exists():
                return ImageFont.truetype(str(candidate), size)
        except OSError:  # unreadable font file — try the next candidate
            continue
    warnings.warn(
        "DejaVu Sans not found; set KESTREL_BRAND_FONT_BOLD / "
        "KESTREL_BRAND_FONT_REGULAR before regenerating brand assets. "
        "Falling back to Pillow's default face.",
        stacklevel=2,
    )
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no sized default
        return ImageFont.load_default()

# The K: geometric, wide-armed, built on a 512 grid.
K_POINTS = [
    (217, 175), (251, 175), (251, 237), (315, 175), (359, 175),
    (282, 254), (365, 337), (321, 337), (251, 269), (251, 337), (217, 337),
]

# For 16–64 px the ring thickens and the K grows, so the silhouette survives.
K_COMPACT = [
    (212, 168), (250, 168), (250, 236), (314, 168), (362, 168),
    (276, 256), (366, 344), (318, 344), (250, 272), (250, 344), (212, 344),
]


# --------------------------------------------------------------------------- helpers
def _gradient(size: tuple[int, int], top: tuple[int, int, int],
              bottom: tuple[int, int, int], diagonal: float = 0.0) -> Image.Image:
    """Linear gradient image, optionally tilted (0 = vertical, 1 = diagonal)."""
    width, height = size
    yy = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    xx = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
    ramp = np.clip(yy * (1.0 - diagonal) + (xx * 0.5 + yy * 0.5) * diagonal, 0, 1)
    ramp = np.repeat(ramp[:, :, None], 3, axis=2)
    top_arr = np.array(top, dtype=np.float32)
    bottom_arr = np.array(bottom, dtype=np.float32)
    rgb = top_arr + (bottom_arr - top_arr) * ramp
    return Image.fromarray(rgb.astype(np.uint8), "RGB")


def _rounded_mask(size: int, radius: int) -> Image.Image:
    scale = size / SIZE
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(radius * scale), fill=255
    )
    return mask


def _arc_gradient_ring(draw: ImageDraw.ImageDraw, box: list[float], width: int,
                       steps: int = 96) -> None:
    """Draw a ring whose colour travels from gold-dark to gold-light."""
    x0, y0, x1, y1 = box
    for index in range(steps):
        start = 360.0 * index / steps - 90.0
        end = 360.0 * (index + 1) / steps - 90.0 + 0.6
        blend = index / max(steps - 1, 1)
        colour = tuple(
            int(GOLD_DARK[channel] + (GOLD_LIGHT[channel] - GOLD_DARK[channel]) * blend)
            for channel in range(3)
        )
        draw.arc([x0, y0, x1, y1], start=start, end=end, fill=colour, width=width)


def _blade_ticks(draw: ImageDraw.ImageDraw, centre: float, inner: float, outer: float,
                 width: int) -> None:
    centre_xy = (centre, centre)
    for blade in range(6):
        angle = math.radians(-90 + blade * 60 + 30)
        start = (
            centre + inner * math.cos(angle),
            centre + inner * math.sin(angle),
        )
        end = (
            centre + outer * math.cos(angle),
            centre + outer * math.sin(angle),
        )
        draw.line([start, end], fill=GOLD_LIGHT, width=width)


# --------------------------------------------------------------------------- mark
def render_mark(px: int = 1024, background: bool = True, detail: bool = True,
                compact: bool = False) -> Image.Image:
    size = px * SS
    scale = size / SIZE

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if background:
        tile = _gradient((size, size), NAVY_TOP, NAVY_BOTTOM, diagonal=0.55).convert("RGBA")
        canvas.paste(tile, (0, 0), _rounded_mask(size, 112))
        # a hairline rim keeps the tile from dissolving into a dark page
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle(
            [2 * scale, 2 * scale, size - 2 * scale, size - 2 * scale],
            radius=int(110 * scale), outline=(44, 60, 88, 255), width=max(2, int(2 * scale)),
        )

    draw = ImageDraw.Draw(canvas)
    centre = 256 * scale
    ring_outer = (180 if compact else 152) * scale
    ring_width = int((24 if compact else 11) * scale)
    _arc_gradient_ring(
        draw,
        [centre - ring_outer, centre - ring_outer, centre + ring_outer, centre + ring_outer],
        width=ring_width,
    )
    if detail and not compact:
        _blade_ticks(draw, centre, inner=136 * scale, outer=150 * scale, width=int(10 * scale))

    shape = K_COMPACT if compact else K_POINTS
    points = [(x * scale, y * scale) for x, y in shape]
    draw.polygon(points, fill=PAPER)

    return canvas.resize((px, px), Image.LANCZOS)


# --------------------------------------------------------------------------- type
def _tracked_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
                  origin: tuple[float, float], tracking: float,
                  fill: tuple[int, int, int]) -> float:
    """Draw letter-spaced text; returns the advance width."""
    x, y = origin
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += font.getlength(char) + tracking
    return x - origin[0] - tracking


def lockup(width: int = 1600, background: str = "dark",
           tagline: str = "AI PHOTO FINISHING FOR ADOBE LIGHTROOM") -> Image.Image:
    height = int(width * 0.30)
    bg = (11, 17, 28) if background == "dark" else (247, 249, 252)
    fg = PAPER if background == "dark" else (16, 24, 38)
    sub = (150, 165, 188) if background == "dark" else (92, 106, 128)

    canvas = Image.new("RGB", (width, height), bg)
    mark_px = int(height * 0.62)
    mark = render_mark(mark_px)
    canvas.paste(mark, (int(height * 0.20), int((height - mark_px) / 2)), mark)

    draw = ImageDraw.Draw(canvas)
    title_font = _font(int(height * 0.30))
    tag_font = _font(max(12, int(height * 0.082)), bold=False)

    text_x = height * 0.20 + mark_px + height * 0.18
    title_y = height * 0.26
    _tracked_text(draw, "KESTREL", title_font, (text_x, title_y),
                  tracking=height * 0.012, fill=fg)

    rule_y = title_y + height * 0.34
    draw.line([(text_x, rule_y), (text_x + width * 0.34, rule_y)],
              fill=GOLD_DARK if background == "dark" else (196, 124, 36), width=max(2, int(height * 0.012)))

    _tracked_text(draw, tagline, tag_font, (text_x, rule_y + height * 0.075),
                  tracking=height * 0.006, fill=sub)
    return canvas


def social_card(width: int = 1280, height: int = 640) -> Image.Image:
    canvas = _gradient((width, height), (10, 15, 26), (22, 33, 54), diagonal=0.7).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    # faint aperture rings, echoing the mark
    for radius, alpha in ((430, 16), (300, 22)):
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw_overlay = ImageDraw.Draw(overlay)
        draw_overlay.ellipse(
            [width - radius * 1.1, height / 2 - radius, width - radius * 1.1 + radius * 2, height / 2 + radius],
            outline=(224, 164, 88, alpha), width=2,
        )
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(canvas)

    mark_px = 300
    mark = render_mark(mark_px)
    canvas.paste(mark, (96, 96), mark)

    title_font = _font(82)
    sub_font = _font(27, bold=False)
    eyebrow_font = _font(20)

    text_x = 96
    _tracked_text(draw, "KESTREL", title_font, (text_x, 388), tracking=3, fill=PAPER)
    draw.line([(text_x, 492), (text_x + 560, 492)], fill=(224, 138, 30), width=3)
    _tracked_text(draw, "AI PHOTO FINISHING FOR ADOBE LIGHTROOM", eyebrow_font,
                  (text_x, 518), tracking=2.4, fill=GOLD_LIGHT)
    _tracked_text(draw, "Sports-first. Explainable. Local.", sub_font,
                  (text_x, 556), tracking=0.6, fill=SLATE)

    badge = _font(18)
    label = "LIGHTROOM CLASSIC"
    width_label = badge.getlength(label)
    box = [width - 96 - width_label - 48, 96, width - 96, 96 + 44]
    draw.rounded_rectangle(box, radius=22, outline=(224, 164, 88), width=2)
    draw.text((box[0] + 24, box[1] + 11), label, font=badge, fill=GOLD_LIGHT)

    return canvas


# --------------------------------------------------------------------------- svg
def mark_svg() -> str:
    points = " ".join(f"{x},{y}" for x, y in K_POINTS)
    ticks = []
    for blade in range(6):
        angle = math.radians(-90 + blade * 60 + 30)
        x1 = 256 + 136 * math.cos(angle)
        y1 = 256 + 136 * math.sin(angle)
        x2 = 256 + 150 * math.cos(angle)
        y2 = 256 + 150 * math.sin(angle)
        ticks.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512" role="img" aria-label="Kestrel">
  <title>Kestrel</title>
  <defs>
    <linearGradient id="k-tile" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0d1423"/>
      <stop offset="1" stop-color="#1c2a44"/>
    </linearGradient>
    <linearGradient id="k-gold" x1="0" y1="1" x2="1" y2="0">
      <stop offset="0" stop-color="#e08a1e"/>
      <stop offset="1" stop-color="#ffc65c"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="112" fill="url(#k-tile)"/>
  <rect x="2" y="2" width="508" height="508" rx="110" fill="none" stroke="#2c3c58" stroke-width="2"/>
  <circle cx="256" cy="256" r="152" fill="none" stroke="url(#k-gold)" stroke-width="11"/>
  <g stroke="#ffc65c" stroke-width="10" stroke-linecap="round">
    {''.join(ticks)}
  </g>
  <polygon points="{points}" fill="#f8fafc"/>
</svg>
"""


def favicon_svg() -> str:
    """A simplified mark for 16–32 px: no ticks, thicker ring, larger K."""
    points = " ".join(f"{x},{y}" for x, y in K_POINTS)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="112" fill="#101a2c"/>
  <circle cx="256" cy="256" r="180" fill="none" stroke="#e0a458" stroke-width="24"/>
  <polygon points="212,168 250,168 250,236 314,168 362,168 276,256 366,344 318,344 250,272 250,344 212,344" fill="#f8fafc"/>
</svg>
"""


def logo_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" width="760" height="200" viewBox="0 0 760 200" role="img" aria-label="Kestrel — AI photo finishing for Adobe Lightroom">
  <title>Kestrel</title>
  <defs>
    <linearGradient id="l-tile" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0d1423"/><stop offset="1" stop-color="#1c2a44"/>
    </linearGradient>
    <linearGradient id="l-gold" x1="0" y1="1" x2="1" y2="0">
      <stop offset="0" stop-color="#e08a1e"/><stop offset="1" stop-color="#ffc65c"/>
    </linearGradient>
  </defs>
  <g transform="translate(20 28) scale(0.28)">
    <rect width="512" height="512" rx="112" fill="url(#l-tile)"/>
    <circle cx="256" cy="256" r="152" fill="none" stroke="url(#l-gold)" stroke-width="11"/>
    <polygon points="217,175 251,175 251,237 315,175 359,175 282,254 365,337 321,337 251,269 251,337 217,337" fill="#f8fafc"/>
  </g>
  <text x="196" y="104" font-family="DejaVu Sans, Segoe UI, Helvetica, Arial, sans-serif" font-size="58" font-weight="700" letter-spacing="3" fill="#f8fafc">KESTREL</text>
  <rect x="198" y="126" width="300" height="3" fill="#e08a1e"/>
  <text x="198" y="158" font-family="DejaVu Sans, Segoe UI, Helvetica, Arial, sans-serif" font-size="15" letter-spacing="2.6" fill="#9aa8be">AI PHOTO FINISHING FOR ADOBE LIGHTROOM</text>
</svg>
"""


# ------------------------------------------------------------------- manifests
# Exactly what `python scripts/brand.py` writes. The manifest is the contract
# used by --check, so a renamed or forgotten asset fails instead of drifting.
ASSET_FILES: dict[str, tuple[int, int] | None] = {
    "kestrel-mark.png": (1024, 1024),
    "kestrel-icon-32.png": (32, 32),
    "kestrel-icon-64.png": (64, 64),
    "kestrel-icon-128.png": (128, 128),
    "kestrel-icon-256.png": (256, 256),
    "kestrel-icon-512.png": (512, 512),
    "kestrel-logo-dark.png": (1600, 480),
    "kestrel-logo-light.png": (1600, 480),
    "social-preview.png": (1280, 640),
    "kestrel-mark.svg": None,
    "kestrel-logo.svg": None,
}

WEB_FILES: dict[str, tuple[int, int] | None] = {
    "brand-mark.svg": None,
    "brand-mark.png": (512, 512),
    "favicon.svg": None,
    "social-preview.png": (1200, 600),
}

# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="verify the committed assets are current without rasterising")
    args = parser.parse_args(argv)
    if args.check:
        return verify()

    BRAND.mkdir(parents=True, exist_ok=True)
    WEB.mkdir(parents=True, exist_ok=True)

    mark = render_mark(1024)
    mark.save(BRAND / "kestrel-mark.png")
    for size in (32, 64, 128, 256, 512):
        icon = render_mark(max(size * 4, 256), detail=size >= 128, compact=size <= 64)
        icon.resize((size, size), Image.LANCZOS).save(BRAND / f"kestrel-icon-{size}.png")

    lockup(1600, "dark").save(BRAND / "kestrel-logo-dark.png")
    lockup(1600, "light").save(BRAND / "kestrel-logo-light.png")
    social_card().save(BRAND / "social-preview.png")

    (BRAND / "kestrel-mark.svg").write_text(mark_svg(), encoding="utf-8")
    (BRAND / "kestrel-logo.svg").write_text(logo_svg(), encoding="utf-8")

    (WEB / "brand-mark.svg").write_text(mark_svg(), encoding="utf-8")
    (WEB / "favicon.svg").write_text(favicon_svg(), encoding="utf-8")
    mark.resize((512, 512), Image.LANCZOS).save(WEB / "brand-mark.png")
    social_card(1200, 600).save(WEB / "social-preview.png")

    print("brand assets written:")
    for name in ASSET_FILES:
        path = BRAND / name
        print(f"  assets/brand/{name}  ({path.stat().st_size / 1024:.0f} KB)")
    for name in WEB_FILES:
        print(f"  web/{name}")
    return 0


def verify() -> int:
    """Fail if the committed assets no longer match the source geometry.

    Rasterised pixels are deliberately *not* compared: PNG bytes depend on the
    Pillow/Pillow-SIMD/zlib/FreeType build and on which DejaVu revision the
    machine has, so a byte diff can never be a portable CI gate. What is
    checked here is everything that is environment independent — the vector
    sources (which are generated from the same coordinates as the bitmaps),
    the manifest of expected files and their pixel dimensions, and the copies
    the web app serves. Regenerating locally and reviewing `git diff` for the
    PNGs stays the documented workflow for art changes.
    """
    problems: list[str] = []

    for root, names in ((BRAND, ASSET_FILES), (WEB, WEB_FILES)):
        for name, size in names.items():
            path = root / name
            if not path.exists():
                problems.append(f"missing {path.relative_to(ROOT)} (run python scripts/brand.py)")
                continue
            if size is None:
                continue
            try:
                with Image.open(path) as image:
                    if image.size != size:
                        problems.append(
                            f"{path.relative_to(ROOT)} is {image.size[0]}×{image.size[1]}, "
                            f"expected {size[0]}×{size[1]}")
            except OSError as exc:
                problems.append(f"{path.relative_to(ROOT)} is not a readable image: {exc}")

    expected_vectors = {
        BRAND / "kestrel-mark.svg": mark_svg(),
        BRAND / "kestrel-logo.svg": logo_svg(),
        WEB / "brand-mark.svg": mark_svg(),
        WEB / "favicon.svg": favicon_svg(),
    }
    for path, expected in expected_vectors.items():
        if not path.exists():
            continue  # already reported above
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            problems.append(f"{path.relative_to(ROOT)} is stale — regenerate with python scripts/brand.py")

    for directory, names in ((BRAND, ASSET_FILES), (WEB, WEB_FILES)):
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name in names:
                continue
            generated_looking = (directory is BRAND or path.name.startswith(("brand-", "favicon", "social-preview")))
            if generated_looking:
                problems.append(
                    f"unexpected file {path.relative_to(ROOT)} — delete it or add it to the manifest")

    if problems:
        print("brand assets are not current:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    total = len(ASSET_FILES) + len(WEB_FILES)
    print(f"brand assets are current ({total} files, geometry and dimensions verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
