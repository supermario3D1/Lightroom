"""Shared fixtures for the Kestrel test suite.

Every test runs against a throw-away KESTREL_HOME, so the developer's library,
database and licence are never touched. Synthetic images are generated here;
they give the suite ground truth without shipping third-party photographs.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

SAMPLE = ROOT / "assets" / "samples" / "football_night.jpg"


# --------------------------------------------------------------------- images
def _fx(h: int, w: int, seed: int = 7) -> np.random.Generator:
    return np.random.default_rng(seed)


def daylight_landscape(width: int = 640, height: int = 420) -> np.ndarray:
    """Blue sky gradient, a sun, a green field and a few dark trees."""
    rng = _fx(height, width)
    y = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    sky = np.zeros((height, width, 3), np.float32)
    sky[..., 0] = (0.16 + 0.22 * (1 - y))[..., 0][:, None]
    sky[..., 1] = (0.34 + 0.30 * (1 - y))[..., 0][:, None]
    sky[..., 2] = (0.66 + 0.28 * (1 - y))[..., 0][:, None]
    horizon = int(height * 0.58)
    ground = rng.random((height - horizon, width, 3)).astype(np.float32)
    sky[horizon:, :, 0] = 0.24 + 0.10 * ground[..., 0]
    sky[horizon:, :, 1] = 0.42 + 0.12 * ground[..., 1]
    sky[horizon:, :, 2] = 0.18 + 0.05 * ground[..., 2]
    # sun
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    dist = np.hypot(xx - width * 0.72, yy - height * 0.20)
    sun = np.clip(1.0 - dist / (min(height, width) * 0.10), 0, 1) ** 1.6
    sky += (sun[..., None] * np.array([0.95, 0.88, 0.70], np.float32))
    # trees
    for cx in (width * 0.18, width * 0.28, width * 0.44):
        blob = np.clip(1.0 - np.hypot(xx - cx, yy - horizon * 1.02) / (height * 0.11), 0, 1) ** 0.8
        colour = np.array([0.10, 0.20, 0.09], np.float32)
        sky = sky * (1 - blob[..., None] * 0.9) + colour * blob[..., None] * 0.9
    return np.clip(sky, 0, 1)


def night_sports(width: int = 640, height: int = 420) -> np.ndarray:
    """Floodlit pitch: bright jersey, green turf, dark crowd, hot lights."""
    rng = _fx(height, width, seed=11)
    img = np.zeros((height, width, 3), np.float32)
    # turf
    img[..., 0] = 0.055
    img[..., 1] = 0.20
    img[..., 2] = 0.07
    img *= (0.9 + 0.2 * rng.random((height, width, 1)).astype(np.float32))
    # crowd band
    img[: int(height * 0.22), :, :] = np.array([0.035, 0.033, 0.045], np.float32)
    # floodlights
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    for cx in (width * 0.15, width * 0.5, width * 0.85):
        dist = np.hypot(xx - cx, yy - height * 0.06)
        glow = np.clip(1.0 - dist / (width * 0.16), 0, 1) ** 2.2
        img += glow[..., None] * np.array([0.55, 0.55, 0.52], np.float32)
    # athlete: torso, shorts, legs, head
    def rect(x0, x1, y0, y1, colour):
        img[int(y0 * height):int(y1 * height), int(x0 * width):int(x1 * width)] = colour

    rect(0.46, 0.56, 0.34, 0.55, np.array([0.86, 0.87, 0.90], np.float32))   # white jersey
    rect(0.46, 0.56, 0.55, 0.66, np.array([0.10, 0.12, 0.45], np.float32))   # navy shorts
    rect(0.475, 0.505, 0.66, 0.80, np.array([0.72, 0.55, 0.44], np.float32))  # leg
    rect(0.515, 0.545, 0.66, 0.80, np.array([0.72, 0.55, 0.44], np.float32))
    rect(0.485, 0.535, 0.245, 0.335, np.array([0.74, 0.57, 0.46], np.float32))  # head
    rect(0.455, 0.565, 0.335, 0.360, np.array([0.74, 0.57, 0.46], np.float32))  # neck
    return np.clip(img, 0, 1)


def flat_patch(width: int = 320, height: int = 240, value=(0.5, 0.5, 0.5)) -> np.ndarray:
    img = np.zeros((height, width, 3), np.float32)
    img[:] = np.array(value, np.float32)
    return img


# --------------------------------------------------------------------- state
@pytest.fixture(scope="session")
def settings(tmp_path_factory):
    from kestrel.core import Settings

    home = tmp_path_factory.mktemp("kestrel-home")
    s = Settings(home=home, default_quality="standard")
    s.ensure_dirs()
    s.save()
    return s


@pytest.fixture(scope="session")
def engine(settings):
    from kestrel.engine import KestrelEngine

    eng = KestrelEngine(settings, start_worker=False)
    yield eng
    try:
        eng.queue.stop()
        eng.close()
    except Exception:
        pass


@pytest.fixture(scope="session")
def sample_path() -> Path:
    return SAMPLE


@pytest.fixture()
def landscape_png(tmp_path, ) -> Path:
    import cv2

    img = daylight_landscape()
    path = tmp_path / "landscape.png"
    ok, buf = cv2.imencode(".png", (img[..., ::-1] * 255).astype(np.uint8))
    assert ok
    path.write_bytes(buf.tobytes())
    return path


@pytest.fixture()
def sports_png(tmp_path) -> Path:
    import cv2

    img = night_sports()
    path = tmp_path / "night_sports.png"
    ok, buf = cv2.imencode(".png", (img[..., ::-1] * 255).astype(np.uint8))
    assert ok
    path.write_bytes(buf.tobytes())
    return path


@pytest.fixture(scope="session")
def imported(engine, sample_path):
    """Import the bundled anonymous sports sample once per session."""
    rec = engine.db.get_image_by_path(str(sample_path.resolve()))
    if rec is not None:
        return rec.id
    result = engine.import_files([str(sample_path)])
    if result["created"]:
        return result["created"][0]["id"]
    return result["skipped"][0]["id"]


@pytest.fixture(scope="session")
def analysis(engine, imported):
    analysis_result, masks, decoded = engine.analyse(imported, quality="standard")
    return analysis_result, masks, decoded
