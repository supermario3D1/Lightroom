"""Intelligent profiles.

A profile is a *brief*, not a preset: it tells the decision engine what the
photographer values (subject emphasis, colour character, skin protection…).  The
engine then interprets that brief per photograph.  Two frames from the same match
can therefore receive different exposure and white-balance numbers while sharing
one coherent look.

Profiles are stored in the local database and can be exported/imported as JSON.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Dict, List, Optional

from ..core import BRAND, ValidationError, new_id, utcnow
from ..models import ProfileSpec


def _p(name: str, genre: str, desc: str, **kw: Any) -> ProfileSpec:
    spec = ProfileSpec(id=_slug(name), name=name, genre=genre, description=desc,
                       builtin=True, created="2026-01-01T00:00:00+00:00",
                       updated="2026-01-01T00:00:00+00:00", **kw)
    return spec


def _slug(name: str) -> str:
    return "builtin:" + "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


BUILTIN_PROFILES: List[ProfileSpec] = [
    # ---------------------------------------------------------------- sports
    _p("Football / Soccer — Match Day", "sports",
       "Publication-grade match day look: lifted athletes, rich natural turf, "
       "accurate kit colour, quietened crowd.",
       subject_emphasis=0.88, background_control=0.78, contrast_character="balanced",
       colour_character="natural", skin_protection=0.92, detail_preservation=0.82,
       grass_enhancement=0.72, uniform_accuracy=0.9, noise_reduction_bias=0.45,
       sharpening_bias=0.72, highlight_priority=0.72, shadow_priority=0.7,
       vignette_amount=-6.0,
       advanced={"grass_boost": 0.35, "crowd_control": 0.10, "condition": "outdoor"}),
    _p("Football / Soccer — Night Match", "sports",
       "Floodlit night football: holds floodlight highlights, cleans up high-ISO "
       "shadow noise, keeps floodlit colour neutral.",
       subject_emphasis=0.94, background_control=0.82, contrast_character="punchy",
       colour_character="natural", skin_protection=0.9, detail_preservation=0.66,
       grass_enhancement=0.6, uniform_accuracy=0.9, noise_reduction_bias=0.85,
       sharpening_bias=0.6, highlight_priority=0.9, shadow_priority=0.55,
       vignette_amount=-9.0,
       advanced={"condition": "night", "crowd_control": 0.18, "wb_tolerance_k": 700,
                 "skin_target_luma": 0.615, "temp_bias_k": -60}),
    _p("Football / Soccer — Grass Priority", "sports",
       "Maximises turf realism and surface texture for pitch-side deliveries.",
       subject_emphasis=0.8, background_control=0.6, contrast_character="balanced",
       grass_enhancement=0.95, uniform_accuracy=0.85, detail_preservation=0.9,
       skin_protection=0.9, advanced={"grass_boost": 0.75, "clarity_bias": 0.15}),
    _p("Basketball — Arena", "sports",
       "Hardwood and painted-key accuracy, protected skin under mixed arena LED, "
       "confident subject separation from the crowd.",
       subject_emphasis=0.9, background_control=0.8, contrast_character="balanced",
       colour_character="natural", skin_protection=0.95, detail_preservation=0.78,
       grass_enhancement=0.0, uniform_accuracy=0.92, noise_reduction_bias=0.6,
       sharpening_bias=0.7, highlight_priority=0.7, shadow_priority=0.65,
       vignette_amount=-7.0,
       advanced={"condition": "indoor", "crowd_control": 0.2, "wb_tolerance_k": 620,
                 "hsl_overrides": {"orange": {"luminance": 1.0}, "yellow": {"saturation": 2.0}}}),
    _p("Basketball — Night Arena", "sports",
       "Night arena basketball: aggressive noise control, neutral LED whites, "
       "punchy kit colours.",
       subject_emphasis=0.95, background_control=0.85, contrast_character="punchy",
       colour_character="natural", skin_protection=0.92, detail_preservation=0.62,
       noise_reduction_bias=0.9, sharpening_bias=0.58, uniform_accuracy=0.92,
       highlight_priority=0.88, shadow_priority=0.5, vignette_amount=-10.0,
       advanced={"condition": "night", "crowd_control": 0.25, "wb_tolerance_k": 760}),
    _p("Australian Rules — AFL Match Day", "sports",
       "Wide grounds, strong sun or overcast: holds both jersey and shorts colour, "
       "keeps turf saturated but natural, tames sun-blown highlights.",
       subject_emphasis=0.86, background_control=0.72, contrast_character="balanced",
       colour_character="natural", skin_protection=0.9, detail_preservation=0.8,
       grass_enhancement=0.8, uniform_accuracy=0.92, noise_reduction_bias=0.4,
       sharpening_bias=0.74, highlight_priority=0.78, shadow_priority=0.66,
       vignette_amount=-5.0,
       advanced={"condition": "outdoor", "grass_boost": 0.45, "crowd_control": 0.12,
                 "dehaze_bias": 0.2}),
    _p("Cricket — Daylight Test", "sports",
       "Long-lens daylight cricket: preserves white clothing and red ball detail, "
       "keeps outfield green credible.",
       subject_emphasis=0.8, background_control=0.62, contrast_character="balanced",
       colour_character="natural", skin_protection=0.92, detail_preservation=0.86,
       grass_enhancement=0.55, uniform_accuracy=0.95, noise_reduction_bias=0.35,
       sharpening_bias=0.72, highlight_priority=0.92, shadow_priority=0.6,
       advanced={"condition": "outdoor", "clarity_bias": 0.1,
                 "hsl_overrides": {"red": {"saturation": 6.0, "luminance": -1.0}}}),
    _p("Rugby — Contact", "sports",
       "Mud, sweat and dark jerseys: opens shadows on faces, keeps skin tones warm "
       "but accurate, adds controlled contrast.",
       subject_emphasis=0.9, background_control=0.7, contrast_character="punchy",
       colour_character="warm", skin_protection=0.88, detail_preservation=0.82,
       grass_enhancement=0.5, uniform_accuracy=0.85, noise_reduction_bias=0.5,
       sharpening_bias=0.72, shadow_priority=0.78, vignette_amount=-6.0),
    _p("Tennis — Court", "sports",
       "Hard-court colour accuracy, crisp whites, clean skin under mixed shade.",
       subject_emphasis=0.84, background_control=0.6, contrast_character="balanced",
       colour_character="natural", skin_protection=0.94, detail_preservation=0.84,
       grass_enhancement=0.25, uniform_accuracy=0.9, noise_reduction_bias=0.35,
       sharpening_bias=0.7, highlight_priority=0.85,
       advanced={"clarity_bias": 0.05}),
    _p("Athletics — Track", "sports",
       "Saturated track and kit, strong subject pop, clean skin under stadium light.",
       subject_emphasis=0.9, background_control=0.68, contrast_character="punchy",
       colour_character="vibrant", skin_protection=0.9, detail_preservation=0.8,
       uniform_accuracy=0.86, noise_reduction_bias=0.45, sharpening_bias=0.74,
       advanced={"temp_bias_k": 60}),
    _p("Swimming — Pool", "sports",
       "Neutralises pool cyan, holds water texture, accurate skin after chlorine cast.",
       subject_emphasis=0.86, background_control=0.7, contrast_character="balanced",
       colour_character="cool", skin_protection=0.95, detail_preservation=0.76,
       uniform_accuracy=0.85, noise_reduction_bias=0.5, sharpening_bias=0.65,
       highlight_priority=0.8,
       advanced={"wb_tolerance_k": 640, "temp_bias_k": 120,
                 "hsl_overrides": {"aqua": {"saturation": -6.0, "hue": 3.0},
                                   "blue": {"saturation": -3.0}}}),
    _p("Indoor Sports — Arena General", "sports",
       "Mixed indoor light: local warm/cool correction, deep noise control, "
       "consistent skin across the frame.",
       subject_emphasis=0.88, background_control=0.78, contrast_character="balanced",
       colour_character="natural", skin_protection=0.93, detail_preservation=0.68,
       noise_reduction_bias=0.8, sharpening_bias=0.62, shadow_priority=0.6),
    _p("Outdoor Sports — Daylight", "sports",
       "Sun and overcast field sport: highlight control, true kit colours, "
       "natural turf.",
       subject_emphasis=0.86, background_control=0.68, contrast_character="balanced",
       colour_character="natural", skin_protection=0.9, detail_preservation=0.82,
       grass_enhancement=0.7, noise_reduction_bias=0.35, sharpening_bias=0.72,
       highlight_priority=0.85,
       advanced={"condition": "outdoor", "dehaze_bias": 0.15}),
    _p("Artificial Stadium Lighting", "sports",
       "Floodlight and LED venues: neutralises green/magenta spikes, protects "
       "highlight roll-off on white kit.",
       subject_emphasis=0.9, background_control=0.8, contrast_character="balanced",
       colour_character="natural", skin_protection=0.92, detail_preservation=0.7,
       noise_reduction_bias=0.78, sharpening_bias=0.64, highlight_priority=0.9,
       advanced={"condition": "artificial", "wb_tolerance_k": 820,
                 "hsl_overrides": {"green": {"saturation": -4.0}}}),

    # ---------------------------------------------------------------- other genres
    _p("Wedding — Editorial Romantic", "wedding",
       "Soft, romantic grade with clean neutrals, protected dress detail and "
       "consistent skin across the day.",
       subject_emphasis=0.55, background_control=0.45, contrast_character="soft",
       colour_character="warm", skin_protection=0.95, detail_preservation=0.75,
       highlight_priority=0.92, shadow_priority=0.8, noise_reduction_bias=0.55,
       sharpening_bias=0.5, vignette_amount=-3.0, advanced={"temp_bias_k": 90}),
    _p("Wedding — Reception & Dance Floor", "wedding",
       "Mixed DJ lighting: local warm/cool correction, deep noise control, "
       "natural skin under colour wash.",
       subject_emphasis=0.6, background_control=0.5, contrast_character="balanced",
       colour_character="natural", skin_protection=0.97, detail_preservation=0.6,
       noise_reduction_bias=0.9, sharpening_bias=0.5, shadow_priority=0.75,
       advanced={"condition": "mixed", "wb_tolerance_k": 700}),
    _p("Portrait — Natural Skin", "portrait",
       "Flattering skin with true texture: eyes crisp, skin sharpening held back, "
       "subtle background falloff.",
       subject_emphasis=0.7, background_control=0.5, contrast_character="balanced",
       colour_character="natural", skin_protection=0.98, detail_preservation=0.78,
       noise_reduction_bias=0.5, sharpening_bias=0.55, vignette_amount=-4.0),
    _p("Portrait — Editorial Colour", "portrait",
       "Magazine grade: filmic curve, gentle grain, confident colour separation.",
       subject_emphasis=0.72, background_control=0.55, contrast_character="editorial",
       colour_character="editorial", skin_protection=0.94, detail_preservation=0.8,
       grain_amount=8.0,
       vignette_amount=-6.0, advanced={"temp_bias_k": 40}),
    _p("Event — Consistent Batch", "event",
       "Consistency-first event delivery: stable colour between frames, mixed-light "
       "correction, clean indoor noise.",
       subject_emphasis=0.62, background_control=0.55, contrast_character="balanced",
       colour_character="natural", skin_protection=0.94, detail_preservation=0.72,
       noise_reduction_bias=0.85, sharpening_bias=0.56, advanced={"wb_tolerance_k": 600}),
    _p("Landscape — Dynamic Range", "landscape",
       "Maximum tonal information: sky recovery, atmospheric depth, true-to-life "
       "colour and no crunchy detail.",
       subject_emphasis=0.0, background_control=0.0, contrast_character="balanced",
       colour_character="natural", skin_protection=0.8, detail_preservation=0.88,
       highlight_priority=0.95, shadow_priority=0.85, sky_enhancement=0.8,
       grass_enhancement=0.35, noise_reduction_bias=0.35, sharpening_bias=0.66,
       advanced={"dehaze_bias": 0.25}),
    _p("Wildlife — Fur & Feathers", "wildlife",
       "Fine detail in fur and feather, quiet background noise, natural animal colour.",
       subject_emphasis=0.8, background_control=0.65, contrast_character="balanced",
       colour_character="natural", skin_protection=0.7, detail_preservation=0.92,
       noise_reduction_bias=0.55, sharpening_bias=0.82, highlight_priority=0.8,
       advanced={"clarity_bias": 0.08}),
    _p("Commercial — Product Accuracy", "commercial",
       "Reproducible colour, clean neutrals, no vignette character on the product, "
       "consistent lighting between shots.",
       subject_emphasis=0.35, background_control=0.25, contrast_character="balanced",
       colour_character="natural", skin_protection=0.85, detail_preservation=0.9,
       noise_reduction_bias=0.3, sharpening_bias=0.7, vignette_amount=0.0),
    _p("General — Everyday Professional", "general",
       "A safe, natural professional treatment for mixed libraries.",
       subject_emphasis=0.55, background_control=0.45, contrast_character="balanced",
       colour_character="natural", skin_protection=0.9, detail_preservation=0.76,
       noise_reduction_bias=0.5, sharpening_bias=0.6),
]


BY_ID: Dict[str, ProfileSpec] = {p.id: p for p in BUILTIN_PROFILES}
BY_GENRE: Dict[str, List[ProfileSpec]] = {}
for _prof in BUILTIN_PROFILES:
    BY_GENRE.setdefault(_prof.genre, []).append(_prof)
for _v in BY_GENRE.values():
    _v.sort(key=lambda p: p.name)


def default_profile_for(genre: str, scene_tags: Optional[List[str]] = None) -> ProfileSpec:
    tags = scene_tags or []
    name_map = {
        "sports": ("night", "Football / Soccer — Night Match") if "night" in tags
        else ("indoor", "Indoor Sports — Arena General") if "indoor" in tags
        else ("outdoor", "Football / Soccer — Match Day"),
        "wedding": (None, "Wedding — Editorial Romantic"),
        "portrait": (None, "Portrait — Natural Skin"),
        "event": (None, "Event — Consistent Batch"),
        "landscape": (None, "Landscape — Dynamic Range"),
        "wildlife": (None, "Wildlife — Fur & Feathers"),
        "commercial": (None, "Commercial — Product Accuracy"),
        "general": (None, "General — Everyday Professional"),
    }
    _, target = name_map.get(genre, (None, "General — Everyday Professional"))
    for p in BUILTIN_PROFILES:
        if p.name == target:
            return p
    return BUILTIN_PROFILES[-1]


def validate_profile(spec: ProfileSpec) -> None:
    if not spec.name.strip():
        raise ValidationError("A profile needs a name.")
    if len(spec.name) > 80:
        raise ValidationError("Profile names are limited to 80 characters.")
    if spec.genre not in ("sports", "wedding", "portrait", "event", "landscape",
                          "wildlife", "commercial", "general"):
        raise ValidationError(f"Unknown genre '{spec.genre}'.")
    for f in ("subject_emphasis", "background_control", "skin_protection",
              "detail_preservation", "noise_reduction_bias", "sharpening_bias",
              "highlight_priority", "shadow_priority", "grass_enhancement",
              "sky_enhancement", "uniform_accuracy"):
        v = getattr(spec, f)
        if not (0.0 <= float(v) <= 1.5):
            raise ValidationError(f"{f.replace('_', ' ')} must be between 0 and 1.5.")


def export_profile(spec: ProfileSpec) -> str:
    payload = {
        "format": "kestrel-profile",
        "version": 1,
        "app": BRAND["name"],
        "exported": utcnow(),
        "profile": spec.to_dict(),
    }
    return json.dumps(payload, indent=2)


def import_profile(text: str) -> ProfileSpec:
    try:
        raw = json.loads(text)
    except Exception as exc:
        raise ValidationError("That file is not valid JSON.", detail=str(exc)) from exc
    if raw.get("format") not in ("kestrel-profile", None):
        raise ValidationError("That file is not a Kestrel profile export.")
    data = raw.get("profile", raw)
    data.pop("id", None)
    known = {f for f in ProfileSpec.__dataclass_fields__}
    spec = ProfileSpec(**{k: v for k, v in data.items() if k in known})
    spec.id = new_id("prof_")
    spec.builtin = False
    spec.created = spec.updated = utcnow()
    if not spec.name:
        spec.name = "Imported profile"
    validate_profile(spec)
    return spec


def clone_profile(spec: ProfileSpec, new_name: Optional[str] = None) -> ProfileSpec:
    clone = replace(spec, id=new_id("prof_"), name=new_name or f"{spec.name} copy",
                    builtin=False, created=utcnow(), updated=utcnow())
    clone.advanced = dict(spec.advanced)
    return clone
