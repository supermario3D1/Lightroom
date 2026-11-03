"""Bundled sample library.

The sample photographs ship with the repository in ``assets/samples`` so that a
fresh clone can demonstrate the engine without importing anything first.  A user
can drop their own demo frames into ``<KESTREL_HOME>/samples`` and they are used
in preference to the bundled ones; the legacy ``data/samples`` location is still
honoured for older installs.

The demo library is optional and clearly labelled in the UI: nothing here does
anything a normal import does not.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("kestrel.demo")

SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff")

COLLECTION_NAME = "Kestrel Sample Library"


def candidate_dirs(home: Path | None = None) -> List[Path]:
    """Where to look for sample photographs, most specific first."""
    repo_root = Path(__file__).resolve().parents[2]
    dirs: List[Path] = []
    if home is not None:
        dirs.append(Path(home) / "samples")
    dirs.append(repo_root / "assets" / "samples")
    dirs.append(repo_root / "data" / "samples")
    return dirs


def sample_dir(home: Path | None = None) -> Path:
    """The first candidate directory that actually contains photographs."""
    for directory in candidate_dirs(home):
        if directory.is_dir() and any(
                p.is_file() and p.suffix.lower() in SUFFIXES for p in directory.iterdir()):
            return directory
    return candidate_dirs(home)[0]


def load_demo_library(eng) -> Dict[str, Any]:
    root = sample_dir(getattr(eng.settings, "home", None))
    files = sorted(p for p in root.glob("*") if p.suffix.lower() in SUFFIXES) if root.is_dir() else []
    if not files:
        return {"created": 0, "error": "No sample photographs are installed.",
                "hint": f"Add a JPEG or TIFF to {root} and retry, or import your own frames.",
                "searched": [str(d) for d in candidate_dirs(getattr(eng.settings, "home", None))]}

    cols = {c["name"]: c for c in eng.db.list_collections()}
    col = cols.get(COLLECTION_NAME)
    if not col:
        created = eng.db.create_collection(
            COLLECTION_NAME, genre_hint="sports",
            notes="Bundled samples demonstrating the engine. Safe to delete.")
        collection_id = created.id
    else:
        collection_id = col["id"]

    result = eng.import_files([str(p) for p in files], collection_id=collection_id)
    return {**result, "collection_id": collection_id, "samples": len(files)}
