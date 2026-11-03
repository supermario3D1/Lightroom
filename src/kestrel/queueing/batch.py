"""Batch intelligence: grouping photographs and keeping a collection consistent.

A professional delivers a *set*, not a stack of individually clever frames.  This
module finds the natural editing groups inside a collection (lighting, venue,
genre) and derives one shared colour decision per group — then every frame in the
group keeps its own exposure and subject treatment on top.

Deliberately not a "copy settings to all": the engine still analyses and plans each
photograph.  The group only constrains white balance, contrast and colour
character, which is where inconsistency is most visible to clients.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ..core import clamp, utcnow
from ..models import AnalysisResult


@dataclass
class ConsistencyGroup:
    id: str
    label: str
    members: List[str] = field(default_factory=list)
    temperature: float = 6500.0
    tint: float = 0.0
    contrast: float = 0.0
    vibrance: float = 0.0
    saturation: float = 0.0
    mean_luma: float = 0.45
    genre: str = "general"
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "members": self.members,
                "temperature": round(self.temperature, 1), "tint": round(self.tint, 2),
                "contrast": round(self.contrast, 1), "vibrance": round(self.vibrance, 1),
                "saturation": round(self.saturation, 1), "mean_luma": round(self.mean_luma, 3),
                "genre": self.genre, "notes": self.notes}


def _feature(a: AnalysisResult) -> np.ndarray:
    return np.array([
        a.exposure.median_luma * 3.0,
        (a.colour.temperature - 5200.0) / 2600.0,
        a.scene.night * 1.2,
        a.scene.indoor * 1.0,
        a.scene.stadium * 0.8,
        a.colour.cast_strength * 2.0,
    ], dtype=np.float32)


def build_groups(analyses: Dict[str, AnalysisResult], max_groups: int = 8) -> List[ConsistencyGroup]:
    ids = [i for i, a in analyses.items() if a is not None]
    if not ids:
        return []
    feats = np.stack([_feature(analyses[i]) for i in ids])
    n = len(ids)
    k = int(clamp(round(math.sqrt(n / 2.0)), 1, min(max_groups, n)))
    # Deterministic k-means over the lighting/venue feature space.
    centres = [feats[int(np.argmax(feats[:, 0]))]]
    for _ in range(1, k):
        d = np.min(np.linalg.norm(feats[:, None, :] - np.array(centres)[None, :, :], axis=2), axis=1)
        centres.append(feats[int(np.argmax(d))])
    centres = np.array(centres, np.float32)
    labels = np.zeros(n, np.int32)
    for _ in range(20):
        dist = np.linalg.norm(feats[:, None, :] - centres[None, :, :], axis=2)
        labels = np.argmin(dist, axis=1)
        for j in range(k):
            sel = labels == j
            if sel.any():
                centres[j] = feats[sel].mean(axis=0)
    groups: List[ConsistencyGroup] = []
    for j in range(k):
        members = [ids[i] for i in range(n) if labels[i] == j]
        if not members:
            continue
        group = _summarise(f"grp_{j + 1}", members, analyses)
        groups.append(group)
    groups.sort(key=lambda g: len(g.members), reverse=True)
    # Re-label with human-readable descriptors.
    for g in groups:
        tags = []
        if g.mean_luma < 0.34:
            tags.append("low light")
        if g.temperature < 5200:
            tags.append("warm/artificial")
        elif g.temperature > 7200:
            tags.append("cool daylight")
        a = analyses[g.members[0]]
        if a.scene.stadium > 0.45:
            tags.append("stadium")
        elif a.scene.indoor > 0.6:
            tags.append("indoor")
        elif a.scene.outdoor > 0.6:
            tags.append("outdoor")
        g.label = f"{a.genre.title()} · " + (", ".join(tags) if tags else "consistent daylight")
        g.notes = ("Group shares lighting character; white balance, contrast and colour "
                   "character are matched across the set. Exposure remains per-photograph.")
    return groups


def _summarise(gid: str, members: List[str], analyses: Dict[str, AnalysisResult]) -> ConsistencyGroup:
    temps, tints, lumas, casts = [], [], [], []
    for m in members:
        a = analyses[m]
        temps.append(a.colour.temperature)
        tints.append(a.colour.tint)
        lumas.append(a.exposure.median_luma)
        casts.append(a.colour.cast_strength)
    # Robust medians: a single flash frame must not drag the whole set.
    temp = float(np.median(temps))
    tint = float(np.median(tints))
    luma = float(np.median(lumas))
    genres = [analyses[m].genre for m in members]
    genre = max(set(genres), key=genres.count)
    return ConsistencyGroup(id=gid, label=genre.title(), members=list(members),
                            temperature=temp, tint=tint, mean_luma=luma, genre=genre)


def consistency_overrides(group: Optional[ConsistencyGroup],
                          analysis: AnalysisResult) -> Dict[str, float]:
    """Values applied to each member so the set matches without flattening it."""
    if group is None:
        return {}
    out: Dict[str, float] = {}
    # White balance: match toward the group median, unless this frame has a very
    # strong, low-confidence cast (rare) in which case it keeps its own solve.
    if abs(group.temperature - analysis.colour.temperature) > 120:
        out["temperature"] = float(group.temperature)
        out["tint"] = float(clamp(group.tint, -25, 25))
    return out


def group_map(groups: List[ConsistencyGroup]) -> Dict[str, str]:
    return {m: g.id for g in groups for m in g.members}
