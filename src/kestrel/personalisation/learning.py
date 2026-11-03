"""Personal editing intelligence — transparent, opt-in, reversible.

Kestrel learns *bounded preferences* (how much contrast, how warm, how much
subject separation) from explicit feedback.  It does not fine-tune models on
customer photographs, and it never changes behaviour silently:

* learning is off unless the photographer enables it and consents;
* every learned adjustment is a small, inspectable number;
* the model can be viewed, exported and reset at any time;
* every feedback event is written to the audit log.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..core import clamp
from ..models import EditPlan, ExplanationItem, ProfileSpec

PREF_KEYS = ("contrast", "vibrance", "saturation", "temperature", "skin_luma",
             "exposure", "sharpness", "denoise", "subject_lift", "background")

MAX_INFLUENCE = {
    "contrast": 0.22, "vibrance": 0.30, "saturation": 0.20, "temperature": 320.0,
    "skin_luma": 0.035, "exposure": 0.20, "sharpness": 0.18, "denoise": 0.18,
    "subject_lift": 0.22, "background": 0.22,
}


@dataclass
class GenrePreference:
    genre: str = "general"
    samples: int = 0
    contrast: float = 0.0
    vibrance: float = 0.0
    saturation: float = 0.0
    temperature: float = 0.0
    skin_luma: float = 0.0
    exposure: float = 0.0
    sharpness: float = 0.0
    denoise: float = 0.0
    subject_lift: float = 0.0
    background: float = 0.0
    last_updated: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Personalisation:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.model: Dict[str, GenrePreference] = {}
        self.load()

    # ------------------------------------------------------------------ state
    def load(self) -> None:
        raw = self.db.get_pref("personalisation.model", {}) or {}
        self.model = {}
        for genre, data in raw.items():
            try:
                self.model[genre] = GenrePreference(**data)
            except Exception:
                continue

    def save(self) -> None:
        self.db.set_pref("personalisation.model",
                         {k: v.to_dict() for k, v in self.model.items()})

    @property
    def enabled(self) -> bool:
        return bool(self.settings.personalised_learning)

    def summary(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "consent": "Learning is opt-in. Kestrel never trains a model on your "
                       "photographs; it stores bounded preference numbers derived from "
                       "your explicit feedback.",
            "genres": {k: v.to_dict() for k, v in self.model.items()},
            "feedback_events": len(self.db.list_feedback(limit=10000)),
            "limits": MAX_INFLUENCE,
        }

    def reset(self) -> None:
        self.model = {}
        self.save()
        self.db.audit("personalisation.reset", {})

    def export_model(self) -> str:
        return json.dumps({"format": "kestrel-personalisation", "version": 1,
                           "model": {k: v.to_dict() for k, v in self.model.items()}},
                          indent=2)

    # ------------------------------------------------------------------ learning
    def _pref(self, genre: str) -> GenrePreference:
        if genre not in self.model:
            self.model[genre] = GenrePreference(genre=genre)
        return self.model[genre]

    def record(self, kind: str, genre: str, *, rating: int = 0,
               manual_delta: Optional[Dict[str, float]] = None,
               plan: Optional[EditPlan] = None) -> Optional[GenrePreference]:
        """Fold one feedback event into the model (bounded exponential average)."""
        if not self.enabled:
            return None
        from ..core import utcnow
        pref = self._pref(genre or "general")
        lr = 1.0 / max(4.0, min(pref.samples + 2, 30.0))     # learning rate

        def nudge(attr: str, target: float, scale: float = 1.0) -> None:
            cur = getattr(pref, attr)
            nxt = cur + (target * scale - cur) * lr
            limit = MAX_INFLUENCE.get(attr, 0.25)
            setattr(pref, attr, float(clamp(nxt, -limit, limit)))

        if kind == "accept":
            nudge("exposure", 0.0, 0.35)
            nudge("contrast", 0.0, 0.35)
        elif kind == "reject":
            # Rejection tells us "less", but not precisely what — nudge toward neutral.
            nudge("contrast", 0.0, 0.6)
            nudge("vibrance", 0.0, 0.6)
        elif kind == "rating":
            value = (int(rating) - 3) / 2.0      # -1 .. +1
            nudge("contrast", 0.10 * value)
            nudge("vibrance", 0.12 * value)
            nudge("saturation", 0.06 * value)
            if plan is not None:
                nudge("temperature", 120.0 * value)
        if manual_delta:
            for key in PREF_KEYS:
                if key not in manual_delta:
                    continue
                v = float(manual_delta[key])
                if key == "temperature" and abs(v) > 0:
                    # manual WB overrides are strong evidence (K)
                    nudge("temperature", float(clamp(v, -600, 600)) * 0.5, 1.0)
                elif key in ("skin_luma",):
                    nudge("skin_luma", float(clamp(v, -0.06, 0.06)))
                else:
                    nudge(key, float(clamp(v / 20.0, -1, 1)))
        pref.samples += 1
        pref.last_updated = utcnow()
        self.save()
        self.db.audit("personalisation.update", {"kind": kind, "genre": genre,
                                                 "samples": pref.samples})
        return pref

    # ------------------------------------------------------------------ apply
    def apply(self, plan: EditPlan, genre: str, profile: ProfileSpec) -> Dict[str, Any]:
        """Apply learned bias to a plan, bounded by MAX_INFLUENCE."""
        if not self.enabled or genre not in self.model:
            return {}
        pref = self.model[genre]
        if pref.samples < 3:
            return {"applied": [], "note": "not enough feedback yet (needs 3 samples)"}
        g = plan.global_
        applied: List[str] = []
        g.contrast = float(clamp(g.contrast + pref.contrast * 30.0, -40, 70))
        g.vibrance = float(clamp(g.vibrance + pref.vibrance * 25.0, -40, 60))
        g.saturation = float(clamp(g.saturation + pref.saturation * 18.0, -30, 30))
        if pref.temperature:
            base = g.temperature if g.temperature is not None else 6500.0
            g.temperature = float(clamp(base + pref.temperature, 3200, 9800))
            applied.append("white balance")
        g.exposure = float(clamp(g.exposure + pref.exposure * 0.4, -2.5, 2.5))
        g.sharpness = float(clamp(g.sharpness + pref.sharpness * 10.0, 25, 95))
        g.luminance_nr = float(clamp(g.luminance_nr + pref.denoise * 15.0, 0, 90))
        lifts = 0
        for m in plan.masks:
            if m.kind == "subject" and pref.subject_lift:
                m.adjustments.exposure = float(clamp(
                    m.adjustments.exposure + pref.subject_lift * 0.25, -1, 1.5))
                lifts += 1
            elif m.kind == "background" and pref.background:
                m.adjustments.exposure = float(clamp(
                    m.adjustments.exposure - pref.background * 0.2, -1.5, 1))
                lifts += 1
        if pref.skin_luma:
            plan.detail["skin_target_luma_bias"] = pref.skin_luma
        applied = ["contrast", "vibrance", "saturation", "exposure", "sharpening",
                   "noise reduction"] + applied
        if lifts:
            applied.append("subject separation")
        plan.explanation.append(ExplanationItem(
            area="Personalisation",
            action="Applied your learned preferences",
            reason=f"Derived from {pref.samples} feedback events for the {genre} genre "
                   "(opt-in, bounded, and reset-able in Settings).",
            amount=", ".join(dict.fromkeys(applied)), confidence=0.7))
        return {"applied": list(dict.fromkeys(applied)), "samples": pref.samples}
