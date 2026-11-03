"""The bundled sample library must survive a fresh clone.

This exists because it did not: the demo loader looked only in the gitignored
``data/samples`` folder, so "Load sample gallery" failed on every clean install
while passing on the developer's machine.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from kestrel import demo

ROOT = Path(__file__).resolve().parents[1]


def test_sample_is_committed_with_the_repository():
    sample = ROOT / "assets" / "samples" / "football_night.jpg"
    assert sample.is_file(), "the demo sample must ship in assets/samples"
    assert sample.stat().st_size > 10_000


def test_sample_dir_finds_the_committed_assets(tmp_path):
    resolved = demo.sample_dir(tmp_path)
    assert resolved.is_dir(), resolved
    assert any(p.suffix.lower() in demo.SUFFIXES for p in resolved.iterdir())


def test_user_samples_take_precedence(tmp_path):
    home_samples = tmp_path / "samples"
    home_samples.mkdir()
    target = home_samples / "mine.jpg"
    shutil.copyfile(ROOT / "assets" / "samples" / "football_night.jpg", target)
    assert demo.sample_dir(tmp_path) == home_samples


def test_empty_home_falls_back_to_the_bundled_assets(tmp_path):
    (tmp_path / "samples").mkdir()
    assert demo.sample_dir(tmp_path) == ROOT / "assets" / "samples"


def test_legacy_data_folder_is_still_searched(tmp_path):
    candidates = demo.candidate_dirs(tmp_path)
    assert (ROOT / "data" / "samples") in candidates
    assert candidates[0] == tmp_path / "samples"


class _FakeDb:
    def __init__(self):
        self.collections = []

    def list_collections(self):
        return self.collections

    def create_collection(self, name, genre_hint="", notes=""):
        class C:
            id = "col_test"
        self.collections.append({"id": "col_test", "name": name})
        return C()


class _FakeSettings:
    def __init__(self, home):
        self.home = home


class _FakeEngine:
    def __init__(self, home):
        self.settings = _FakeSettings(home)
        self.db = _FakeDb()
        self.imported = []

    def import_files(self, paths, collection_id=None):
        self.imported = list(paths)
        return {"created": [{"id": "img_test"}], "skipped": [], "failed": [],
                "collection_id": collection_id}


def test_demo_import_uses_the_bundled_sample(tmp_path):
    eng = _FakeEngine(tmp_path)
    result = demo.load_demo_library(eng)
    assert result["samples"] >= 1
    assert result["collection_id"] == "col_test"
    assert any(p.endswith("football_night.jpg") for p in eng.imported)


def test_demo_import_reports_where_it_looked_when_empty(tmp_path):
    eng = _FakeEngine(tmp_path)
    eng.settings.home = tmp_path / "nothing-here"
    original = demo.candidate_dirs

    def only_empty(_home=None):
        return [tmp_path / "empty"]

    demo.candidate_dirs = only_empty
    try:
        result = demo.load_demo_library(eng)
    finally:
        demo.candidate_dirs = original
    assert result["created"] == 0
    assert "searched" in result
    assert "No sample photographs" in result["error"]
