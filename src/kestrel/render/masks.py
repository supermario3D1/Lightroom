"""Persistent mask storage.

Masks are saved as 8-bit mattes keyed by the analysis hash, so a render can be
reproduced exactly after a restart without re-running analysis (the expensive
part), and edits are never lost if the process dies mid-batch.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Iterable, Optional

import cv2
import numpy as np

MASK_KINDS = ("subject", "background", "face", "skin", "sky", "grass", "court",
              "crowd", "clothing", "warm_light", "cool_light", "highlight_detail")


class MaskStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Dict[str, np.ndarray]] = {}

    @staticmethod
    def key_for(analysis_json: str, sizes: tuple) -> str:
        h = hashlib.sha256(analysis_json.encode()).hexdigest()[:16]
        return f"{h}_{sizes[0]}x{sizes[1]}"

    def _path(self, key: str, kind: str) -> Path:
        return self.root / f"{key}__{kind}.png"

    def save(self, key: str, masks: Dict[str, Optional[np.ndarray]]) -> None:
        for kind, mask in masks.items():
            if mask is None:
                continue
            arr = np.clip(mask * 255.0, 0, 255).astype(np.uint8)
            ok, buf = cv2.imencode(".png", arr, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            if not ok:
                continue
            p = self._path(key, kind)
            tmp = p.with_suffix(".png.tmp")
            tmp.write_bytes(buf.tobytes())
            tmp.replace(p)

    def load(self, key: str, shape: tuple) -> Dict[str, np.ndarray]:
        if key in self._cache:
            return self._cache[key]
        out: Dict[str, np.ndarray] = {}
        for kind in MASK_KINDS:
            p = self._path(key, kind)
            if not p.exists():
                continue
            img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            if img.shape[:2] != shape[:2]:
                img = cv2.resize(img, (shape[1], shape[0]), interpolation=cv2.INTER_LINEAR)
            out[kind] = (img.astype(np.float32) / 255.0)
        if len(self._cache) > 12:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = out
        return out

    def has(self, key: str) -> bool:
        return self._path(key, "subject").exists() or self._path(key, "face").exists()

    def delete(self, key: str) -> None:
        for f in self.root.glob(f"{key}__*.png"):
            try:
                f.unlink()
            except OSError:
                pass
        self._cache.pop(key, None)

    def cleanup(self, keep_keys: Iterable[str]) -> int:
        keep = set(keep_keys)
        removed = 0
        for f in self.root.glob("*.png"):
            key = f.name.split("__")[0]
            if key not in keep:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


def resize_mask(mask: np.ndarray, shape: tuple) -> np.ndarray:
    if mask.shape[:2] == shape[:2]:
        return mask
    return cv2.resize(mask, (shape[1], shape[0]), interpolation=cv2.INTER_LINEAR)


def combine_masks(base: Optional[np.ndarray], add: Optional[np.ndarray],
                  subtract: Optional[np.ndarray] = None) -> Optional[np.ndarray]:
    out = base
    if add is not None:
        out = add if out is None else np.maximum(out, add)
    if subtract is not None and out is not None:
        out = out * (1.0 - np.clip(subtract, 0, 1))
    return out
