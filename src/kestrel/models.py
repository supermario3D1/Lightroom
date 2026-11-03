"""Versioned data contracts shared by the analyser, decision engine, renderer,
API, database and Lightroom plugin.

All numbers that end up in :class:`GlobalAdjust` are expressed in **Lightroom
develop-setting units**, so an edit plan is simultaneously:

* an executable recipe for Kestrel's own high-bit-depth renderer, and
* a set of native, editable Lightroom sliders that can be applied non-destructively.
"""
from __future__ import annotations

import hashlib
import json
import dataclasses
from dataclasses import dataclass, field, asdict, fields
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .core import BRAND, utcnow

SCHEMA_VERSION = BRAND["engine_schema"]


def _plain(obj: Any) -> Any:
    """Recursively convert dataclasses / numpy scalars into JSON-safe values."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_plain(v) for v in obj]
    if isinstance(obj, (str, bool)) or obj is None:
        return obj
    if hasattr(obj, "item") and not isinstance(obj, (bytes, bytearray)):
        try:
            return obj.item()
        except Exception:
            return obj
    if isinstance(obj, (int, float)):
        return obj
    return obj


def _d(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return obj


def _filter_kwargs(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in (raw or {}).items() if k in known}


class _JsonMixin:
    def to_dict(self) -> Dict[str, Any]:
        return _plain(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    def hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- analysis
@dataclass
class FaceBox(_JsonMixin):
    x: float
    y: float
    w: float
    h: float
    confidence: float = 0.9
    skin_ratio: float = 0.0
    mean_luma: float = 0.5
    sharpness: float = 0.0
    yaw: float = 0.0          # -1 left .. +1 right (estimated)
    eye_open: float = 0.5

    @property
    def area(self) -> float:
        return self.w * self.h

    @property
    def center(self) -> Tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2


@dataclass
class SubjectRegion(_JsonMixin):
    """A semantically meaningful region and its soft coverage."""
    kind: str                       # subject | background | sky | grass | crowd | skin | face
    coverage: float = 0.0           # fraction of frame
    mean_luma: float = 0.5
    mean_rgb: List[float] = field(default_factory=lambda: [0.5, 0.5, 0.5])
    mean_lab: List[float] = field(default_factory=lambda: [50.0, 0.0, 0.0])
    confidence: float = 0.5
    bbox: Optional[List[float]] = None
    notes: str = ""


@dataclass
class ExposureInfo(_JsonMixin):
    mean_luma: float = 0.45
    median_luma: float = 0.45
    p01: float = 0.0
    p05: float = 0.02
    p25: float = 0.25
    p75: float = 0.75
    p95: float = 0.95
    p99: float = 1.0
    clipping_high: float = 0.0       # fraction of pixels above 0.995
    clipping_low: float = 0.0        # fraction below 0.005
    headroom_high: float = 0.0       # stops of recoverable highlight
    headroom_low: float = 0.0
    subject_luma: float = 0.45
    face_luma: float = 0.5
    background_luma: float = 0.45
    ev_bias: float = 0.0             # estimated EV error of the capture
    dynamic_range_stops: float = 8.0
    histogram: List[float] = field(default_factory=list)


@dataclass
class ColourInfo(_JsonMixin):
    temperature: float = 6500.0
    tint: float = 0.0
    cast_strength: float = 0.0
    cast_confidence: float = 0.0
    wb_confidence: float = 0.5
    saturation_mean: float = 0.25
    saturation_p95: float = 0.55
    vibrance_need: float = 0.0
    chroma_noise: float = 0.0
    dominant_colours: List[List[float]] = field(default_factory=list)
    skin_hue: float = 25.0
    skin_saturation: float = 0.35
    skin_lab: List[float] = field(default_factory=lambda: [62.0, 14.0, 20.0])
    grey_balance: float = 0.0
    is_monochrome: bool = False


@dataclass
class QualityInfo(_JsonMixin):
    noise_sigma: float = 0.0           # 0..1 in display units
    iso_estimate: int = 100
    noise_luma: float = 0.0
    noise_chroma: float = 0.0
    noise_shadow_bias: float = 0.0
    sharpness: float = 0.5             # 0..1 normalised
    focus_plane: str = "unknown"       # subject | background | uniform
    motion_blur: float = 0.0
    compression_artifacts: float = 0.0
    banding: float = 0.0
    bit_depth: int = 8
    is_raw: bool = False
    lens: Dict[str, Any] = field(default_factory=dict)
    camera: Dict[str, Any] = field(default_factory=dict)
    quality_score: float = 0.7


@dataclass
class SceneInfo(_JsonMixin):
    indoor: float = 0.5
    outdoor: float = 0.5
    night: float = 0.0
    artificial: float = 0.0
    stadium: float = 0.0
    mixed_light: float = 0.0
    backlit: float = 0.0
    overcast: float = 0.0
    snowfall_or_rain: float = 0.0
    horizon_tilt: float = 0.0
    sky_coverage: float = 0.0
    crowd_coverage: float = 0.0
    grass_coverage: float = 0.0
    court_coverage: float = 0.0
    event_tags: List[str] = field(default_factory=list)
    genre_scores: Dict[str, float] = field(default_factory=dict)
    venue_signature: Dict[str, float] = field(default_factory=dict)


@dataclass
class AnalysisResult(_JsonMixin):
    image_id: str = ""
    width: int = 0
    height: int = 0
    orientation: str = "landscape"
    created: str = field(default_factory=utcnow)
    schema: int = SCHEMA_VERSION
    exposure: ExposureInfo = field(default_factory=ExposureInfo)
    colour: ColourInfo = field(default_factory=ColourInfo)
    quality: QualityInfo = field(default_factory=QualityInfo)
    scene: SceneInfo = field(default_factory=SceneInfo)
    faces: List[FaceBox] = field(default_factory=list)
    regions: Dict[str, SubjectRegion] = field(default_factory=dict)
    subject_coverage: float = 0.0
    subject_confidence: float = 0.0
    subject_bbox: Optional[List[float]] = None
    genre: str = "general"
    genre_confidence: float = 0.0
    notes: List[str] = field(default_factory=list)
    timings_ms: Dict[str, float] = field(default_factory=dict)
    preview_hash: str = ""
    analysis_edge: int = 1600
    providers: Dict[str, str] = field(default_factory=dict)

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1e6

    def primary_faces(self, min_area: float = 0.0035, rel: float = 0.30) -> List["FaceBox"]:
        """Faces that are large enough to be *subject* faces rather than crowd faces.

        A 3-pixel face in the stand must not set the whole frame's skin target.
        """
        if not self.faces:
            return []
        biggest = max(f.area for f in self.faces)
        thr = max(min_area, biggest * rel)
        picked = [f for f in self.faces if f.area >= thr]
        return picked or [max(self.faces, key=lambda f: f.area)]


# --------------------------------------------------------------------------- adjustments
@dataclass
class GlobalAdjust(_JsonMixin):
    """Lightroom develop-setting units. Defaults == no change."""
    exposure: float = 0.0          # EV, -5..5
    contrast: float = 0.0          # -100..100
    highlights: float = 0.0
    shadows: float = 0.0
    whites: float = 0.0
    blacks: float = 0.0
    texture: float = 0.0
    clarity: float = 0.0
    dehaze: float = 0.0
    vibrance: float = 0.0
    saturation: float = 0.0
    temperature: Optional[float] = None   # Kelvin; None = leave as shot
    tint: Optional[float] = None
    sharpness: float = 40.0
    sharpen_radius: float = 1.0
    sharpen_detail: float = 25.0
    sharpen_masking: float = 0.0
    luminance_nr: float = 0.0
    luminance_detail: float = 50.0
    color_nr: float = 25.0
    color_detail: float = 50.0
    lens_corrections: bool = True
    remove_ca: bool = True
    defringe: float = 0.0
    post_crop_vignette: float = 0.0
    grain: float = 0.0
    curve_rgb: Optional[List[int]] = None
    curve_red: Optional[List[int]] = None
    curve_blue: Optional[List[int]] = None
    hsl_hue: Dict[str, float] = field(default_factory=dict)
    hsl_saturation: Dict[str, float] = field(default_factory=dict)
    hsl_luminance: Dict[str, float] = field(default_factory=dict)
    profile: Optional[str] = None

    def is_noop(self) -> bool:
        d = self.to_dict()
        skip = {"sharpness", "sharpen_radius", "sharpen_detail", "sharpen_masking",
                "luminance_detail", "color_nr", "color_detail", "lens_corrections",
                "remove_ca", "profile", "temperature", "tint"}
        return all(abs(float(v)) < 0.5 for k, v in d.items()
                   if k not in skip and isinstance(v, (int, float))) and \
            all(abs(v) < 0.5 for v in list(self.hsl_hue.values()) + list(self.hsl_saturation.values()) +
                list(self.hsl_luminance.values()))

    def scaled(self, factor: float) -> "GlobalAdjust":
        """Blend toward neutral — used by the editing-strength control."""
        out = GlobalAdjust(**_filter_kwargs(GlobalAdjust, self.to_dict()))
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, float):
                neutral = 0.0
                if f.name in ("sharpness", "sharpen_radius", "sharpen_detail"):
                    neutral = {"sharpness": 40.0, "sharpen_radius": 1.0,
                               "sharpen_detail": 25.0}[f.name]
                elif f.name in ("luminance_detail", "color_detail"):
                    neutral = 50.0
                elif f.name == "color_nr":
                    neutral = 25.0
                setattr(out, f.name, neutral + (v - neutral) * factor)
        out.hsl_hue = {k: v * factor for k, v in self.hsl_hue.items()}
        out.hsl_saturation = {k: v * factor for k, v in self.hsl_saturation.items()}
        out.hsl_luminance = {k: v * factor for k, v in self.hsl_luminance.items()}
        return out


@dataclass
class MaskLayer(_JsonMixin):
    """A local adjustment. Geometry is normalised to the *cropped* frame (0..1)."""
    name: str
    kind: str = "subject"          # subject|background|face|skin|sky|grass|clothing|crowd|radial|linear|object
    enabled: bool = True
    inverted: bool = False
    feather: float = 0.35
    opacity: float = 1.0
    geometry: Dict[str, Any] = field(default_factory=dict)
    adjustments: GlobalAdjust = field(default_factory=GlobalAdjust)
    confidence: float = 0.8
    provenance: str = "ai"         # ai | ai-refined | manual
    notes: str = ""


@dataclass
class ExplanationItem(_JsonMixin):
    area: str
    action: str
    reason: str
    amount: str = ""
    confidence: float = 0.8


@dataclass
class EditMetrics(_JsonMixin):
    exposure_error: float = 0.0
    clipping_high: float = 0.0
    clipping_low: float = 0.0
    contrast: float = 0.0
    saturation: float = 0.0
    skin_delta_e: float = 0.0
    skin_consistency: float = 0.0
    noise_level: float = 0.0
    sharpness: float = 0.0
    haloing: float = 0.0
    naturalness: float = 0.0
    subject_lift: float = 0.0
    background_lift: float = 0.0
    score: float = 0.0


@dataclass
class EditPlan(_JsonMixin):
    image_id: str = ""
    mode: str = "adaptive"          # adaptive (engine render) | native (Lightroom sliders)
    genre: str = "general"
    intent: str = "automatic"
    strength: float = 1.0
    quality: str = "high"
    global_: GlobalAdjust = field(default_factory=GlobalAdjust)
    masks: List[MaskLayer] = field(default_factory=list)
    explanation: List[ExplanationItem] = field(default_factory=list)
    metrics: EditMetrics = field(default_factory=EditMetrics)
    denoise: Dict[str, Any] = field(default_factory=dict)
    sharpening: Dict[str, Any] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)
    iterations: int = 1
    crops: Dict[str, Any] = field(default_factory=dict)
    schema: int = SCHEMA_VERSION
    engine_version: str = BRAND["version"]
    created: str = field(default_factory=utcnow)
    analysis_hash: str = ""
    preview_hash: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out = super().to_dict()
        out["global"] = out.pop("global_")
        return out

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EditPlan":
        raw = dict(raw or {})
        g = raw.pop("global", raw.pop("global_", {})) or {}
        masks = []
        for m in raw.pop("masks", []) or []:
            mm = dict(m)
            adj = mm.pop("adjustments", {}) or {}
            layer = MaskLayer(**_filter_kwargs(MaskLayer, mm))
            layer.adjustments = (GlobalAdjust(**_filter_kwargs(GlobalAdjust, adj))
                                 if isinstance(adj, dict) else adj)
            masks.append(layer)
        expl = [ExplanationItem(**_filter_kwargs(ExplanationItem, e))
                for e in raw.pop("explanation", []) or []]
        met = EditMetrics(**_filter_kwargs(EditMetrics, raw.pop("metrics", {}) or {}))
        plan = cls(**_filter_kwargs(cls, raw))
        plan.global_ = GlobalAdjust(**_filter_kwargs(GlobalAdjust, g))
        plan.masks = masks
        plan.explanation = expl
        plan.metrics = met
        return plan

    def settings_summary(self) -> List[Dict[str, Any]]:
        """Human-readable list of the global sliders that actually moved."""
        labels = {
            "exposure": ("Exposure", "EV"), "contrast": ("Contrast", ""),
            "highlights": ("Highlights", ""), "shadows": ("Shadows", ""),
            "whites": ("Whites", ""), "blacks": ("Blacks", ""),
            "texture": ("Texture", ""), "clarity": ("Clarity", ""),
            "dehaze": ("Dehaze", ""), "vibrance": ("Vibrance", ""),
            "saturation": ("Saturation", ""), "temperature": ("Temperature", "K"),
            "tint": ("Tint", ""), "sharpness": ("Sharpening", ""),
            "luminance_nr": ("Noise reduction", ""), "color_nr": ("Colour NR", ""),
        }
        g = self.global_
        out = []
        for key, (label, unit) in labels.items():
            if key in ("sharpness", "luminance_nr", "color_nr"):
                continue  # reported separately in the detail panel
            v = getattr(g, key)
            neutral = None if key in ("temperature", "tint") else 0.0
            if v is None:
                continue
            if neutral is not None and abs(v) < 0.05:
                continue
            out.append({"key": key, "label": label, "value": round(float(v), 2),
                        "unit": unit, "display": f"{v:+.2f}" if isinstance(v, float) else str(v)})
        return out


# --------------------------------------------------------------------------- library
@dataclass
class Collection(_JsonMixin):
    id: str = ""
    name: str = "Untitled collection"
    genre_hint: str = ""
    created: str = field(default_factory=utcnow)
    notes: str = ""
    profile_id: Optional[str] = None


@dataclass
class ImageRecord(_JsonMixin):
    id: str = ""
    path: str = ""
    filename: str = ""
    collection_id: Optional[str] = None
    lr_uuid: Optional[str] = None
    width: int = 0
    height: int = 0
    bytes: int = 0
    sha256: str = ""
    format: str = ""
    imported: str = field(default_factory=utcnow)
    status: str = "new"            # new | analysed | edited | failed | reviewed
    genre: str = ""
    analysis: Optional[AnalysisResult] = None
    plan: Optional[EditPlan] = None
    current_version_id: Optional[str] = None
    edition_count: int = 0
    error: str = ""
    camera: str = ""
    lens: str = ""
    iso: int = 0
    rating: int = 0
    accepted: Optional[bool] = None
    tags: List[str] = field(default_factory=list)
    render_report: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ImageRecord":
        raw = dict(raw or {})
        a = raw.pop("analysis", None)
        p = raw.pop("plan", None)
        rec = cls(**_filter_kwargs(cls, raw))
        if a and isinstance(a, dict):
            aa = dict(a)
            for sub, klass in (("exposure", ExposureInfo), ("colour", ColourInfo),
                               ("quality", QualityInfo), ("scene", SceneInfo)):
                if isinstance(aa.get(sub), dict):
                    aa[sub] = klass(**_filter_kwargs(klass, aa[sub]))
            aa["faces"] = [FaceBox(**_filter_kwargs(FaceBox, f)) for f in aa.get("faces", []) or []]
            aa["regions"] = {k: SubjectRegion(**_filter_kwargs(SubjectRegion, v))
                             for k, v in (aa.get("regions") or {}).items()}
            rec.analysis = AnalysisResult(**_filter_kwargs(AnalysisResult, aa))
        if p and isinstance(p, dict):
            rec.plan = EditPlan.from_dict(p)
        return rec


@dataclass
class Version(_JsonMixin):
    """Immutable record of one edit (never overwritten, never lost on crash)."""
    id: str = ""
    image_id: str = ""
    parent_id: Optional[str] = None
    created: str = field(default_factory=utcnow)
    mode: str = "adaptive"
    intent: str = "automatic"
    strength: float = 1.0
    quality: str = "high"
    profile_id: Optional[str] = None
    plan: Optional[EditPlan] = None
    render_path: str = ""
    thumb_path: str = ""
    original_thumb: str = ""
    report: Dict[str, Any] = field(default_factory=dict)
    applied_to_lightroom: bool = False
    feedback: Optional[Dict[str, Any]] = None
    note: str = ""


@dataclass
class ProfileSpec(_JsonMixin):
    """An *intelligent* profile: preferences the AI interprets, not fixed sliders."""
    id: str = ""
    name: str = "New profile"
    genre: str = "general"
    intent: str = "automatic"
    description: str = ""
    builtin: bool = False
    created: str = field(default_factory=utcnow)
    updated: str = field(default_factory=utcnow)
    # Behaviour weights (0..1.5 style knobs)
    subject_emphasis: float = 0.65
    background_control: float = 0.5
    contrast_character: str = "balanced"        # soft|balanced|punchy|editorial
    colour_character: str = "natural"           # natural|warm|cool|editorial|vibrant
    saturation_bias: float = 0.0
    vibrance_bias: float = 0.0
    skin_protection: float = 0.85
    detail_preservation: float = 0.7
    noise_reduction_bias: float = 0.5
    sharpening_bias: float = 0.6
    highlight_priority: float = 0.6
    shadow_priority: float = 0.6
    grass_enhancement: float = 0.4
    sky_enhancement: float = 0.35
    uniform_accuracy: float = 0.8
    grain_amount: float = 0.0
    vignette_amount: float = 0.0
    crop_suggestion: bool = False
    advanced: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JobRecord(_JsonMixin):
    id: str = ""
    kind: str = "edit"              # edit | analyse | render | batch
    image_ids: List[str] = field(default_factory=list)
    collection_id: Optional[str] = None
    status: str = "queued"          # queued|running|paused|done|failed|cancelled
    priority: int = 5
    progress: float = 0.0
    total: int = 0
    completed: int = 0
    failed: int = 0
    created: str = field(default_factory=utcnow)
    started: Optional[str] = None
    finished: Optional[str] = None
    eta_seconds: Optional[float] = None
    message: str = ""
    options: Dict[str, Any] = field(default_factory=dict)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    worker_pid: Optional[int] = None
