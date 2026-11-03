"""Image decoding, colour management and preview generation.

Supported input: JPEG, PNG, TIFF (8/16-bit), WebP, and RAW via LibRaw (CR2/CR3,
NEF, ARW, DNG, RAF, ORF, RW2, PEF, SRW, …).  Originals are never modified.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
from PIL import Image, ImageCms, ImageOps

from . import color
from .core import ProcessingError, UnsupportedImage, atomic_write_bytes

RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng",
                  ".raf", ".orf", ".rw2", ".pef", ".srw", ".raw", ".rwl", ".3fr",
                  ".fff", ".iiq", ".mos", ".mrw", ".x3f"}
STILL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".heic"}
SUPPORTED_EXTENSIONS = RAW_EXTENSIONS | STILL_EXTENSIONS


@dataclass
class ImageMetadata:
    camera: str = ""
    lens: str = ""
    iso: int = 0
    exposure_time: str = ""
    aperture: str = ""
    focal_length: str = ""
    white_balance: str = ""
    colour_space: str = ""
    bit_depth: int = 8
    is_raw: bool = False
    orientation: int = 1
    width: int = 0
    height: int = 0
    as_shot_temperature: Optional[float] = None
    as_shot_tint: Optional[float] = None
    software: str = ""
    captured: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class DecodedImage:
    rgb: np.ndarray                 # display-encoded float32, 0..1, HxWx3
    meta: ImageMetadata
    scale: float = 1.0              # rgb resolution / original resolution
    source: str = ""

    @property
    def height(self) -> int:
        return int(self.rgb.shape[0])

    @property
    def width(self) -> int:
        return int(self.rgb.shape[1])


def is_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# --------------------------------------------------------------------------- EXIF
def _ratio_to_float(v: Any) -> float:
    try:
        if isinstance(v, tuple) and len(v) == 2 and v[1]:
            return float(v[0]) / float(v[1])
        return float(v)
    except Exception:
        return 0.0


def _parse_exif(img: Image.Image) -> Dict[str, Any]:
    try:
        ex = img.getexif()
        if not ex:
            return {}
        out: Dict[str, Any] = {}
        for tag, name in ((271, "make"), (272, "model"), (305, "software"),
                          (42036, "lens"), (37386, "focal"), (33434, "shutter"),
                          (33437, "aperture"), (34855, "iso"), (36867, "captured")):
            if tag in ex:
                out[name] = ex.get(tag)
        # White balance tags live in the Exif IFD
        for tag, name in ((41987, "wb"), (41986, "exposure_mode")):
            try:
                if tag in ex:
                    out[name] = ex.get(tag)
            except Exception:
                pass
        return out
    except Exception:
        return {}


def _icc_to_srgb(img: Image.Image) -> Tuple[Image.Image, str]:
    try:
        icc = img.info.get("icc_profile")
        if not icc:
            return img, "assumed-sRGB"
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        desc = (src.profile.profile_description or "").strip()
        dst = ImageCms.createProfile("sRGB")
        if "srgb" in desc.lower():
            return img, desc or "sRGB"
        converted = ImageCms.profileToProfile(img, src, dst, outputMode="RGB")
        return converted, desc or "embedded"
    except Exception:
        return img, "unreadable-embedded"


def _to_float_array(img: Image.Image) -> np.ndarray:
    mode = img.mode
    if mode in ("I;16", "I;16B", "I;16L", "I"):
        arr = np.asarray(img, dtype=np.uint16 if mode != "I" else np.int32)
        scale = 65535.0 if mode != "I" else max(float(arr.max()), 1.0)
        return (arr.astype(np.float32) / scale)
    if mode == "F":
        return np.clip(np.asarray(img, np.float32), 0, 1)
    if mode != "RGB":
        img = img.convert("RGB")
    return np.asarray(img, np.float32) / 255.0


# --------------------------------------------------------------------------- RAW
def _decode_raw(path: Path) -> Tuple[np.ndarray, ImageMetadata]:
    try:
        import rawpy
    except Exception as exc:  # pragma: no cover
        raise UnsupportedImage(
            "RAW support requires the rawpy package.",
            detail=str(exc), hint="Install Kestrel with `pip install rawpy`.") from exc
    try:
        with rawpy.imread(str(path)) as raw:
            sizes = raw.sizes
            wb = raw.camera_whitebalance
            meta = ImageMetadata(is_raw=True, bit_depth=16,
                                 width=sizes.width, height=sizes.height)
            # LibRaw (via rawpy) exposes sensor geometry and white balance, but it
            # does not surface the EXIF capture fields in a stable API. Rather than
            # invent them, only fields we can actually read are filled in; Lightroom
            # supplies the rest through the plug-in (see lightroom/).
            meta.colour_space = "raw-camera"
            try:
                meta.camera = str(getattr(raw, "camera_model", "") or "").strip()
            except Exception:
                meta.camera = ""
            if len(wb) >= 3:
                try:
                    # Camera WB (r, g, b, g2) → approximate correlated colour temp
                    rgb_wb = np.array([wb[0], wb[1], wb[2]], dtype=np.float64)
                    rgb_wb = rgb_wb / max(rgb_wb[1], 1e-6)
                    xyz = (rgb_wb / np.max(rgb_wb)) @ np.linalg.inv(color.SRGB_TO_XYZ).T
                    if xyz[0] + xyz[1] + xyz[2] > 0:
                        x = xyz[0] / xyz.sum()
                        y = xyz[1] / xyz.sum()
                        cct, _ = color.xy_to_cct_tint(float(np.clip(x, 0.2, 0.5)),
                                                      float(np.clip(y, 0.2, 0.45)))
                        meta.as_shot_temperature = round(cct / 50) * 50
                except Exception:
                    pass
            # Linear light, 16-bit, camera WB, sRGB primaries — the honest start point.
            rgb16 = raw.postprocess(
                use_camera_wb=True, no_auto_bright=True, output_color=rawpy.ColorSpace.sRGB,
                output_bps=16, gamma=(1, 1), user_flip=None, highlight_mode=rawpy.HighlightMode.Clip,
                fbdd_noise_reduction=rawpy.FBDDNoiseReductionMode.Off, demosaic_algorithm=None
                if not hasattr(rawpy, "DemosaicAlgorithm") else rawpy.DemosaicAlgorithm.AHD,
            )
        linear = rgb16.astype(np.float32) / 65535.0
        # highlight reconstruction: soft-roll values that clip in one or two channels
        linear = _highlight_rolloff(linear)
        display = color.linear_to_srgb(linear)
        return display, meta
    except UnsupportedImage:
        raise
    except Exception as exc:
        raise ProcessingError(f"Could not decode RAW file {path.name}.",
                              detail=str(exc),
                              hint="The file may be corrupt or use an unsupported RAW variant.") from exc


def _highlight_rolloff(linear: np.ndarray, knee: float = 0.94) -> np.ndarray:
    """Preserve highlight hue when one channel clips before the others."""
    lum = linear @ color.LUMA.astype(np.float32)
    over = lum > knee
    if not np.any(over):
        return linear
    out = linear.copy()
    scale = np.ones_like(lum)
    scale[over] = knee / np.maximum(lum[over], 1e-5)
    # keep relative channel ratios, then soft-compress the top end
    out[over] = linear[over] * scale[over][:, None]
    return np.clip(out, 0.0, 1.0)


# --------------------------------------------------------------------------- stills
def _decode_still(path: Path) -> Tuple[np.ndarray, ImageMetadata]:
    try:
        img = Image.open(path)
        img.load()
    except Exception as exc:
        raise ProcessingError(f"Could not open {path.name}.", detail=str(exc),
                              hint="Check that the file is a complete, valid image.") from exc
    exif = _parse_exif(img)
    meta = ImageMetadata(
        camera=" ".join(str(exif.get(k, "")) for k in ("make", "model") if exif.get(k)).strip(),
        lens=str(exif.get("lens", "") or ""),
        iso=int(_ratio_to_float(exif.get("iso", 0))) if exif.get("iso") else 0,
        software=str(exif.get("software", "") or ""),
        captured=str(exif.get("captured", "") or ""),
        bit_depth=16 if img.mode.startswith("I;16") else 8,
        width=img.width, height=img.height,
    )
    st = _ratio_to_float(exif.get("shutter", 0))
    if st:
        meta.exposure_time = f"1/{int(round(1 / st))}" if st < 1 else f"{st:.1f}s"
    ap = _ratio_to_float(exif.get("aperture", 0))
    if ap:
        meta.aperture = f"f/{ap:.1f}"
    fl = _ratio_to_float(exif.get("focal", 0))
    if fl:
        meta.focal_length = f"{fl:.0f}mm"
    wb = exif.get("wb")
    meta.white_balance = {0: "Auto", 1: "Daylight"}.get(wb if isinstance(wb, int) else -1, "")
    img, profile = _icc_to_srgb(img)
    meta.colour_space = profile
    img = ImageOps.exif_transpose(img)
    arr = _to_float_array(img)
    if arr.ndim == 2:
        arr = np.repeat(arr[..., None], 3, axis=2)
    if arr.shape[2] > 3:
        arr = arr[..., :3]
    meta.orientation = 1
    meta.width, meta.height = int(arr.shape[1]), int(arr.shape[0])
    if meta.bit_depth == 8:
        # 8-bit JPEGs carry sRGB transfer by convention; treat as display data.
        pass
    return np.ascontiguousarray(arr, dtype=np.float32), meta


def _resize(img: np.ndarray, max_edge: Optional[int]) -> Tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    if not max_edge or max(h, w) <= max_edge:
        return img, 1.0
    scale = max_edge / float(max(h, w))
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    import cv2
    out = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    return out, scale


def decode(path: str | Path, max_edge: Optional[int] = None) -> DecodedImage:
    p = Path(path)
    if not p.exists():
        raise ProcessingError(f"File not found: {p.name}", detail=str(p),
                              hint="Re-import the image or check that the drive is connected.")
    ext = p.suffix.lower()
    if ext in RAW_EXTENSIONS:
        arr, meta = _decode_raw(p)
    elif ext in STILL_EXTENSIONS:
        arr, meta = _decode_still(p)
    else:
        raise UnsupportedImage(f"{ext or 'Unknown'} files are not supported.",
                              hint="Kestrel accepts JPEG, PNG, TIFF, WebP and camera RAW files.")
    arr, scale = _resize(arr, max_edge)
    return DecodedImage(rgb=np.ascontiguousarray(arr, np.float32), meta=meta,
                        scale=scale, source=str(p))


# --------------------------------------------------------------------------- encoding
def encode_jpeg(rgb: np.ndarray, quality: int = 88, icc: bool = True,
                max_edge: Optional[int] = None) -> bytes:
    arr = np.clip(rgb, 0, 1)
    if max_edge:
        arr, _ = _resize(arr, max_edge)
    buf = io.BytesIO()
    img = Image.fromarray((arr * 255.0 + 0.5).astype(np.uint8), "RGB")
    params: Dict[str, Any] = {"format": "JPEG", "quality": int(quality),
                              "optimize": True, "progressive": True,
                              "subsampling": 0 if quality >= 90 else 2}
    if icc:
        try:
            params["icc_profile"] = ImageCms.ImageCmsProfile(
                ImageCms.createProfile("sRGB")).tobytes()
        except Exception:
            pass
    img.save(buf, **params)
    return buf.getvalue()


def srgb_icc_bytes() -> bytes:
    """Embedded sRGB ICC profile (LittleCMS built-in, ~600 bytes)."""
    try:
        return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    except Exception:
        return b""


def write_tiff16(rgb: np.ndarray, path: str | Path, icc: bytes | None = None,
                 rows_per_strip: int = 64) -> Path:
    """Write a 16-bit RGB TIFF with an embedded sRGB ICC profile.

    Implemented directly (baseline TIFF, Adobe deflate, strips) because neither
    Pillow nor OpenCV can tag a 16-bit RGB TIFF with a colour profile — and an
    untagged 16-bit file is exactly what causes colour surprises downstream.
    """
    import struct
    import zlib

    arr = (np.clip(np.asarray(rgb, np.float32), 0, 1) * 65535.0 + 0.5).astype("<u2")
    h, w = arr.shape[:2]
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    icc = icc if icc is not None else srgb_icc_bytes()

    strips = []
    for y0 in range(0, h, rows_per_strip):
        chunk = arr[y0:y0 + rows_per_strip].tobytes()
        strips.append(zlib.compress(chunk, 6))

    entries = []          # (tag, type, count, value/offset placeholder)
    extra = bytearray()

    def add(tag, typ, count, payload):
        entries.append((tag, typ, count, payload))

    # layout: header(8) | strip data | icc data | bitsperSample array | IFD
    data_offset = 8
    strip_offsets = []
    pos = data_offset
    strip_blob = bytearray()
    for sdata in strips:
        strip_offsets.append(pos)
        strip_blob += sdata
        pos += len(sdata)
    icc_offset = pos
    icc_blob = icc or b""
    bits_offset = icc_offset + len(icc_blob)
    bits_blob = struct.pack("<3H", 16, 16, 16)
    ifd_offset = bits_offset + len(bits_blob)

    add(256, 4, 1, w)                 # ImageWidth (LONG)
    add(257, 4, 1, h)                 # ImageLength
    add(258, 3, 3, bits_offset)       # BitsPerSample (SHORT[3] out of line)
    add(259, 3, 1, 8)                 # Compression: Adobe deflate
    add(262, 3, 1, 2)                 # PhotometricInterpretation: RGB
    add(273, 4, len(strip_offsets), 0)  # StripOffsets (patched below)
    add(277, 3, 1, 3)                 # SamplesPerPixel
    add(278, 4, 1, rows_per_strip)    # RowsPerStrip
    add(279, 4, len(strips), 0)       # StripByteCounts (patched below)
    add(282, 5, 1, 0)                 # XResolution -> rational (patched)
    add(283, 5, 1, 0)                 # YResolution
    add(284, 3, 1, 1)                 # PlanarConfiguration: chunky
    add(296, 3, 1, 2)                 # ResolutionUnit: inch
    software = b"Kestrel\x00"
    add(305, 2, len(software), software)        # Software (ASCII, NUL-terminated)
    if icc_blob:
        add(34675, 7, len(icc_blob), icc_offset)  # ICC profile (UNDEFINED)

    # out-of-line values that need their own space: strip offsets, byte counts,
    # rationals, and the software string.
    n_entries = len(entries)
    ifd_size = 2 + n_entries * 12 + 4
    cursor = ifd_offset + ifd_size
    # strip offset array
    so_offset = cursor
    so_blob = struct.pack(f"<{len(strip_offsets)}I", *strip_offsets)
    cursor += len(so_blob)
    sb_offset = cursor
    sb_blob = struct.pack(f"<{len(strips)}I", *[len(x) for x in strips])
    cursor += len(sb_blob)
    res_off = cursor
    res_blob = struct.pack("<2I", 300, 1) * 2     # 300/1 for X and Y
    cursor += 16
    sw_off = cursor
    sw_blob = b"Kestrel\x00"
    cursor += len(sw_blob)

    # Patch entries. A single strip fits inline in the IFD, so the *value* is the
    # strip offset itself; with several strips the IFD stores an offset to the
    # array. Getting this wrong produces a file that only some readers accept.
    fixed = []
    for (tag, typ, count, payload) in entries:
        if tag == 273:
            fixed.append((tag, typ, count,
                          strip_offsets[0] if len(strip_offsets) == 1 else so_offset))
        elif tag == 279:
            fixed.append((tag, typ, count,
                          len(strips[0]) if len(strips) == 1 else sb_offset))
        elif tag == 282:
            fixed.append((tag, typ, count, res_off))
        elif tag == 283:
            fixed.append((tag, typ, count, res_off + 8))
        elif tag == 305:
            fixed.append((tag, typ, count, sw_off))
        else:
            fixed.append((tag, typ, count, payload))
    ifd = bytearray(struct.pack("<H", len(fixed)))
    type_sizes = {2: 1, 3: 2, 4: 4, 5: 8, 7: 1}
    for (tag, typ, count, payload) in fixed:
        size = type_sizes[typ] * count
        if size <= 4 and not isinstance(payload, (bytes, bytearray)):
            if typ == 3:
                val = struct.pack("<HH", payload & 0xFFFF, 0)[:4]
            else:
                val = struct.pack("<I", int(payload))
        else:
            val = struct.pack("<I", int(payload))
        ifd += struct.pack("<HHI", tag, typ, count) + val
    ifd += struct.pack("<I", 0)   # next IFD

    with open(out, "wb") as fh:
        fh.write(b"II")
        fh.write(struct.pack("<HI", 42, ifd_offset))
        fh.write(bytes(strip_blob))
        fh.write(icc_blob)
        fh.write(bits_blob)
        fh.write(bytes(ifd))
        fh.write(so_blob)
        fh.write(sb_blob)
        fh.write(res_blob)
        fh.write(sw_blob)
    return out


def encode_png(rgb: np.ndarray, bit_depth: int = 8) -> bytes:
    arr = np.clip(rgb, 0, 1)
    buf = io.BytesIO()
    if bit_depth == 16:
        import cv2
        bgr16 = (arr * 65535.0 + 0.5).astype(np.uint16)[..., ::-1]
        ok, enc = cv2.imencode(".png", np.ascontiguousarray(bgr16), [cv2.IMWRITE_PNG_COMPRESSION, 6])
        if not ok:
            raise ProcessingError("Could not encode the 16-bit PNG.")
        return enc.tobytes()
    if True:
        img = Image.fromarray((arr * 255.0 + 0.5).astype(np.uint8), "RGB")
        img.save(buf, format="PNG", optimize=True, compress_level=6)
    return buf.getvalue()


def save_preview(rgb: np.ndarray, path: str | Path, max_edge: int = 1400,
                 quality: int = 90) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, encode_jpeg(rgb, quality=quality, max_edge=max_edge))
    return out


def load_preview_bytes(path: str | Path) -> bytes:
    return Path(path).read_bytes()


def thumbnail_from(rgb: np.ndarray, size: int = 320) -> bytes:
    return encode_jpeg(rgb, quality=78, max_edge=size)


def average_colour(rgb: np.ndarray, mask: np.ndarray | None = None) -> Tuple[float, float, float]:
    if mask is not None and mask.any():
        sel = rgb[mask]
    else:
        sel = rgb.reshape(-1, 3)
    if sel.size == 0:
        return (0.5, 0.5, 0.5)
    m = sel.mean(axis=0)
    return float(m[0]), float(m[1]), float(m[2])


def file_fingerprint(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    st = p.stat()
    return {"size": st.st_size, "mtime": int(st.st_mtime), "name": p.name}
