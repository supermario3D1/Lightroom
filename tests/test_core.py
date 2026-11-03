"""Core contracts: settings, quality presets, errors, helpers."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from kestrel.core import (BRAND, QUALITY_PRESETS, HardwareProfile, NotFound,
                          ProcessingError, Settings, ValidationError, clamp,
                          human_seconds, new_id, utcnow)


def test_brand_is_stable():
    assert BRAND["name"] == "Kestrel"
    assert BRAND["version"].count(".") == 2
    assert isinstance(BRAND["engine_schema"], int)


def test_settings_create_directories(tmp_path):
    s = Settings(home=tmp_path / "home")
    s.ensure_dirs()
    for path in (s.storage_dir, s.import_dir, s.render_dir, s.mask_dir, s.preview_dir):
        assert path.is_dir(), path


def test_settings_round_trip(tmp_path, monkeypatch):
    s = Settings(home=tmp_path / "home", default_quality="maximum", telemetry=False)
    s.ensure_dirs()
    s.save()
    stored = json.loads(s.config_path.read_text())
    assert stored["default_quality"] == "maximum"
    assert stored["telemetry"] is False
    monkeypatch.setenv("KESTREL_HOME", str(s.home))
    again = Settings.load()
    assert again.default_quality == "maximum"
    assert again.home == s.home


def test_quality_presets_are_ordered():
    standard, high, maximum = (QUALITY_PRESETS[k] for k in ("standard", "high", "maximum"))
    assert standard["passes"] <= high["passes"] <= maximum["passes"]
    assert standard["analysis_edge"] <= maximum["analysis_edge"]
    assert standard["preview_edge"] <= maximum["preview_edge"]
    for preset in (standard, high, maximum):
        assert preset["preview_edge"] >= 800


def test_quality_lookup_falls_back(tmp_path):
    s = Settings(home=tmp_path, default_quality="high")
    assert s.quality("maximum")["analysis_edge"] == QUALITY_PRESETS["maximum"]["analysis_edge"]
    fallback = s.quality("nonsense")
    assert fallback["analysis_edge"] == QUALITY_PRESETS["high"]["analysis_edge"]
    assert s.quality(None)["analysis_edge"] == QUALITY_PRESETS["high"]["analysis_edge"]


def test_error_types_carry_actionable_payload():
    err = ProcessingError("Could not read the file.", detail="bad header",
                          hint="Try re-exporting it.")
    payload = err.to_dict()
    assert payload["error"] in ("processing_error", "processing_failed")
    assert payload["message"].startswith("Could not read")
    assert payload["hint"].startswith("Try re-exporting")
    assert 400 <= err.http_status < 600
    assert NotFound("missing").http_status == 404
    assert 400 <= ValidationError("bad").http_status < 500


def test_clamp_is_numpy_aware():
    assert clamp(3.0, 0.0, 1.0) == 1.0
    arr = np.array([-2.0, 0.5, 9.0], dtype=np.float32)
    out = clamp(arr, 0.0, 1.0)
    assert out.dtype == np.float32
    assert out.tolist() == [0.0, 0.5, 1.0]


def test_human_seconds():
    assert human_seconds(None) == "no estimate"
    assert human_seconds(30) == "30s"
    assert human_seconds(90) == "1m 30s"
    assert human_seconds(3600) == "1h 00m"


def test_ids_are_unique_and_prefixed():
    ids = {new_id("img_") for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("img_") for i in ids)


def test_utcnow_is_iso8601():
    stamp = utcnow()
    assert stamp.endswith("+00:00")
    assert "T" in stamp


def test_hardware_profile_detects_without_gpu():
    hw = HardwareProfile.detect()
    assert hw.cpu_count >= 1
    assert hw.total_memory_gb > 0
    payload = hw.to_dict()
    assert "onnx_providers" in payload
    assert isinstance(payload["onnx_providers"], list)
    assert isinstance(payload["onnx_runtime"], bool)
    if payload["onnx_runtime"]:
        assert payload["onnx_providers"], "ONNX Runtime is installed but lists no providers"
    else:
        assert payload["onnx_providers"] == [], "providers were reported without a runtime"
    assert payload["cuda"] in (True, False)


def test_telemetry_and_cloud_default_off(tmp_path):
    s = Settings(home=tmp_path)
    assert s.telemetry is False
    assert s.allow_cloud is False
    assert s.personalised_learning is False
