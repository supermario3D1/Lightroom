"""Image I/O: decoding, encoding, 16-bit TIFF with a real ICC profile."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from kestrel import imageio


def test_supported_extensions():
    assert imageio.is_supported("shot.JPG")
    assert imageio.is_supported("shot.cr3")
    assert imageio.is_supported("shot.dng")
    assert not imageio.is_supported("notes.txt")


def test_decode_resizes_and_keeps_aspect(sample_path):
    full = imageio.decode(str(sample_path))
    small = imageio.decode(str(sample_path), max_edge=400)
    assert max(small.rgb.shape[:2]) == 400
    assert small.rgb.dtype == np.float32
    assert small.rgb.min() >= 0.0 and small.rgb.max() <= 1.0 + 1e-6
    ratio_full = full.rgb.shape[0] / full.rgb.shape[1]
    ratio_small = small.rgb.shape[0] / small.rgb.shape[1]
    assert abs(ratio_full - ratio_small) < 0.02
    assert small.meta.width >= 400


def test_decode_missing_file_raises_notfound(tmp_path):
    from kestrel.core import KestrelError

    with pytest.raises(KestrelError):
        imageio.decode(str(tmp_path / "nope.jpg"))


def test_encode_jpeg_round_trip(tmp_path, sample_path):
    decoded = imageio.decode(str(sample_path), max_edge=320)
    data = imageio.encode_jpeg(decoded.rgb, quality=90)
    path = tmp_path / "out.jpg"
    path.write_bytes(data)
    again = imageio.decode(str(path))
    assert again.rgb.shape[:2] == decoded.rgb.shape[:2]
    assert float(np.abs(again.rgb - decoded.rgb).mean()) < 0.02


def test_encode_png_16bit(tmp_path):
    import cv2

    arr = np.linspace(0, 1, 64 * 48 * 3, dtype=np.float32).reshape(48, 64, 3)
    data = imageio.encode_png(arr, bit_depth=16)
    path = tmp_path / "gradient.png"
    path.write_bytes(data)
    assert path.stat().st_size > 400
    # OpenCV reads the file without converting it to 8 bits.
    raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    assert raw.dtype == np.uint16, raw.dtype
    assert raw.shape[:2] == (48, 64)
    assert float(np.abs(raw.astype(np.float32) / 65535.0 - arr[..., ::-1]).max()) < 2e-3
    decoded = imageio.decode(str(path))
    assert decoded.rgb.shape[:2] == (48, 64)


def test_srgb_icc_is_a_valid_profile():
    icc = imageio.srgb_icc_bytes()
    assert isinstance(icc, bytes) and len(icc) > 300
    # ICC profile header: size, 'acsp' signature at offset 36, RGB colour space
    size = struct.unpack(">I", icc[:4])[0]
    assert size == len(icc)
    assert icc[36:40] == b"acsp"
    assert icc[16:20] == b"RGB "


def test_write_tiff16_is_16bit_rgb_with_icc(tmp_path):
    rgb = np.linspace(0.0, 1.0, 96 * 64 * 3, dtype=np.float32).reshape(64, 96, 3)
    path = tmp_path / "delivery.tif"
    imageio.write_tiff16(rgb, path, icc=imageio.srgb_icc_bytes())
    assert path.exists()

    with Image.open(path) as img:
        assert img.size == (96, 64)
        assert img.tag_v2[258] in ((16, 16, 16), 16)     # BitsPerSample
        assert img.tag_v2[277] == 3                      # SamplesPerPixel
        assert img.tag_v2[259] in (8, 32946)             # deflate compression
        icc = img.info.get("icc_profile")
        assert icc and len(icc) > 300
        img.load()

    import cv2

    pixels = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    assert pixels.dtype == np.uint16
    assert pixels.shape[:2] == (64, 96)
    # value round-trip: the gradient must survive 16-bit quantisation
    assert int(pixels[0, 0, 0]) <= 8
    assert int(pixels[-1, -1, -1]) >= 65527


def test_thumbnail_from_preserves_aspect(sample_path):
    decoded = imageio.decode(str(sample_path), max_edge=400)
    thumb = imageio.thumbnail_from(decoded.rgb, size=96)
    assert thumb[:2] == b"\xff\xd8"    # JPEG SOI
    tmp = Path(__file__).parent / "_thumb.jpg"
    try:
        tmp.write_bytes(thumb)
        assert imageio.decode(str(tmp)).rgb.shape[0] <= 96
    finally:
        tmp.unlink(missing_ok=True)


def test_sha256_file_is_stable(sample_path):
    first = imageio.sha256_file(sample_path)
    second = imageio.sha256_file(sample_path)
    assert first == second and len(first) == 64


def test_save_preview_writes_jpeg(tmp_path, sample_path):
    decoded = imageio.decode(str(sample_path), max_edge=400)
    path = tmp_path / "preview.jpg"
    imageio.save_preview(decoded.rgb, path, max_edge=200)
    loaded = imageio.load_preview_bytes(path)
    assert loaded[:2] == b"\xff\xd8"
