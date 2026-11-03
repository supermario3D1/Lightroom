"""Editing intents: how a photographer's chosen 'look' scales the recipe.

Intents never change *what* is detected or protected — only the strength and
character of the treatment.  Skin protection and highlight safety are never
reduced below safe limits by an intent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from ..models import EditPlan


@dataclass(frozen=True)
class Intent:
    name: str
    label: str
    contrast: float = 1.0
    clarity: float = 1.0
    vibrance: float = 1.0
    saturation: float = 1.0
    shadows: float = 1.0
    highlights: float = 1.0
    whites: float = 1.0
    detail: float = 1.0
    vignette: float = 1.0
    grain: float = 0.0
    warmth: float = 0.0        # Kelvin bias applied to the WB solve
    wb_amount: float = 0.8
    curve: str = ""            # "" | filmic
    description: str = ""


INTENTS: Dict[str, Intent] = {
    "automatic": Intent(
        name="automatic", label="Automatic",
        description="The engine chooses the treatment it believes is best for this photograph."),
    "natural": Intent(
        name="natural", label="Natural", contrast=0.72, clarity=0.62, vibrance=0.82,
        saturation=0.75, shadows=1.12, highlights=1.18, whites=0.8, detail=0.9,
        vignette=0.5, wb_amount=0.65,
        description="Understated, true-to-life colour and soft contrast; safest for skin."),
    "vibrant": Intent(
        name="vibrant", label="Vibrant", contrast=1.08, clarity=1.02, vibrance=1.32,
        saturation=1.18, shadows=0.95, highlights=1.0, whites=1.05, detail=1.05,
        vignette=1.0, wb_amount=0.8,
        description="Rich, punchy colour for social and club delivery."),
    "dramatic": Intent(
        name="dramatic", label="Dramatic", contrast=1.38, clarity=1.32, vibrance=0.98,
        saturation=1.0, shadows=0.62, highlights=1.15, whites=1.1, detail=1.06,
        vignette=1.7, grain=0.28, wb_amount=0.85,
        description="Deep shadows, strong separation and a moodier grade."),
    "editorial": Intent(
        name="editorial", label="Editorial", contrast=1.14, clarity=1.05, vibrance=0.9,
        saturation=0.9, shadows=1.05, highlights=1.3, whites=0.95, detail=1.0,
        vignette=0.9, grain=0.2, warmth=120.0, wb_amount=0.9, curve="filmic",
        description="Magazine-style grade: controlled highlights, filmic curve, gentle grain."),
    "custom": Intent(
        name="custom", label="Custom", description="Manual overrides; AI provides the base analysis."),
}


def get_intent(name: str) -> Intent:
    return INTENTS.get((name or "automatic").lower(), INTENTS["automatic"])


def with_strength(plan: EditPlan, strength: float) -> EditPlan:
    """Blend an entire plan toward neutral by the user's editing-strength control."""
    s = max(0.0, min(1.5, float(strength)))
    plan.global_ = plan.global_.scaled(s)
    for layer in plan.masks:
        layer.opacity *= min(1.0, s)
        layer.adjustments = layer.adjustments.scaled(s)
    plan.strength = s
    return plan
