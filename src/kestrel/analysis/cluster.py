"""Tiny deterministic clustering helpers (no scikit-learn dependency)."""
from __future__ import annotations

from typing import List, Sequence, Tuple

import numpy as np


def kmeans_simple(points: np.ndarray, k: int = 2, iterations: int = 16,
                  max_points: int = 6000) -> List[Tuple[List[float], float]]:
    """Deterministic k-means with farthest-point seeding.

    Returns ``[(centre, weight_fraction), ...]`` sorted by weight descending.
    Deterministic seeding matters: the same photograph must produce the same edit.
    """
    pts = np.asarray(points, np.float32).reshape(-1, 3)
    if pts.shape[0] == 0:
        return []
    if pts.shape[0] > max_points:
        idx = np.linspace(0, pts.shape[0] - 1, max_points).astype(int)
        pts = pts[idx]
    k = int(max(1, min(k, pts.shape[0])))
    # farthest-point seeding from the brightest sample (stable, no RNG)
    lum = pts @ np.array([0.299, 0.587, 0.114], np.float32)
    centres = [pts[int(np.argmax(lum))]]
    for _ in range(1, k):
        d = np.min(np.linalg.norm(pts[:, None, :] - np.array(centres)[None, :, :], axis=2), axis=1)
        centres.append(pts[int(np.argmax(d))])
    centres = np.array(centres, np.float32)
    labels = np.zeros(pts.shape[0], np.int32)
    for _ in range(iterations):
        dist = np.linalg.norm(pts[:, None, :] - centres[None, :, :], axis=2)
        labels = np.argmin(dist, axis=1)
        for j in range(k):
            sel = labels == j
            if sel.any():
                centres[j] = pts[sel].mean(axis=0)
    out = []
    for j in range(k):
        sel = labels == j
        if sel.sum() == 0:
            continue
        out.append(([float(v) for v in centres[j]], float(sel.mean())))
    out.sort(key=lambda t: t[1], reverse=True)
    return out
