"""Kestrel core: branding, configuration, logging and shared errors.

Everything in Kestrel is local-first.  No network call is made unless the user
explicitly enables an online feature (update check / licence activation).
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# --------------------------------------------------------------------------------------
# Brand
# --------------------------------------------------------------------------------------
BRAND = {
    "name": "Kestrel",
    "legal_name": "Kestrel Imaging",
    "tagline": "A professional editor's eye, on every frame.",
    "description": "AI photo editing engine for Adobe Lightroom Classic.",
    "bundle_id": "com.kestrelimaging.lightroom",
    "support_email": "support@kestrelimaging.example",
    "website": "https://kestrelimaging.example",
    "version": "1.0.0",
    "channel": "release-candidate",
    "api_version": "v1",
    "engine_schema": 3,
}
APP_NAME = BRAND["name"]
APP_VERSION = BRAND["version"]


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------
class KestrelError(Exception):
    """Base class for user-facing errors."""

    code = "kestrel.error"
    http_status = 500

    def __init__(self, message: str, *, detail: Optional[str] = None,
                 hint: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.hint = hint

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "message": self.message,
                "detail": self.detail, "hint": self.hint}


class NotFound(KestrelError):
    code = "not_found"
    http_status = 404


class ValidationError(KestrelError):
    code = "invalid_request"
    http_status = 400


class ProcessingError(KestrelError):
    code = "processing_failed"
    http_status = 422


class UnsupportedImage(KestrelError):
    code = "unsupported_image"
    http_status = 415


class LicenceError(KestrelError):
    code = "licence_required"
    http_status = 402


# --------------------------------------------------------------------------------------
# Paths and configuration
# --------------------------------------------------------------------------------------
def default_home() -> Path:
    env = os.environ.get("KESTREL_HOME")
    if env:
        return Path(env).expanduser().resolve()
    # Repository-local data directory keeps Kestrel fully portable.
    root = Path(__file__).resolve().parents[2]
    return (root / "data").resolve()


@dataclass
class HardwareProfile:
    """Detected capabilities, used to choose providers and tiling."""
    cpu_count: int = 1
    cpu_name: str = "unknown"
    total_memory_gb: float = 4.0
    gpu_name: Optional[str] = None
    cuda: bool = False
    metal: bool = False
    onnx_providers: list = field(default_factory=list)
    onnx_runtime: bool = False       # is onnxruntime importable at all?
    checked_at: str = ""

    @classmethod
    def detect(cls) -> "HardwareProfile":
        prof = cls()
        try:
            import psutil
            prof.cpu_count = psutil.cpu_count(logical=True) or os.cpu_count() or 1
            prof.total_memory_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        except Exception:
            prof.cpu_count = os.cpu_count() or 1
        prof.cpu_name = platform.processor() or platform.machine()
        try:
            import onnxruntime as ort
            prof.onnx_runtime = True
            prof.onnx_providers = list(ort.get_available_providers())
            prof.cuda = "CUDAExecutionProvider" in prof.onnx_providers
            prof.metal = "CoreMLExecutionProvider" in prof.onnx_providers
        except Exception:
            # No ONNX Runtime: the engine runs on its classical backends, and we
            # say so rather than implying a provider list exists.
            prof.onnx_runtime = False
            prof.onnx_providers = []
        prof.checked_at = utcnow()
        return prof

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


QUALITY_PRESETS = {
    # analysis_edge / preview_edge / passes / nlm / deconv / full-res refinement
    "standard": {"analysis_edge": 1280, "preview_edge": 1400, "passes": 1,
                 "noise_refine": False, "deconvolution": False,
                 "label": "Standard", "est_seconds": 4},
    "high": {"analysis_edge": 1600, "preview_edge": 1800, "passes": 2,
             "noise_refine": True, "deconvolution": False,
             "label": "High", "est_seconds": 12},
    "maximum": {"analysis_edge": 2048, "preview_edge": 2200, "passes": 3,
                "noise_refine": True, "deconvolution": True,
                "label": "Maximum", "est_seconds": 30},
}


@dataclass
class Settings:
    home: Path = field(default_factory=default_home)
    host: str = "0.0.0.0"
    port: int = 8765
    default_quality: str = "high"
    default_intent: str = "automatic"
    max_workers: int = 1
    memory_budget_gb: float = 1.6
    tile_pixels: int = 12_000_000
    preserve_input_profile: bool = True
    write_sidecars: bool = False            # opt-in XMP local-adjust sidecars (beta)
    keep_intermediate_tiffs: bool = False
    telemetry: bool = False                 # never on by default
    allow_cloud: bool = False               # never on by default
    personalised_learning: bool = False     # requires explicit consent
    retention_days: int = 90
    log_level: str = "INFO"
    update_channel: str = "stable"
    last_update_check: str = ""
    user_name: str = ""
    onboarding_complete: bool = False

    # ---- derived paths ----
    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def db_path(self) -> Path:
        return self.home / "kestrel.db"

    @property
    def storage_dir(self) -> Path:
        return self.home / "storage"

    @property
    def preview_dir(self) -> Path:
        return self.storage_dir / "previews"

    @property
    def render_dir(self) -> Path:
        return self.storage_dir / "renders"

    @property
    def mask_dir(self) -> Path:
        return self.storage_dir / "masks"

    @property
    def import_dir(self) -> Path:
        return self.storage_dir / "imports"

    @property
    def log_path(self) -> Path:
        return self.home / "kestrel.log"

    @property
    def backup_dir(self) -> Path:
        return self.home / "backups"

    def ensure_dirs(self) -> None:
        for p in (self.home, self.storage_dir, self.preview_dir, self.render_dir,
                  self.mask_dir, self.import_dir, self.backup_dir):
            p.mkdir(parents=True, exist_ok=True)

    def quality(self, name: Optional[str] = None) -> Dict[str, Any]:
        q = QUALITY_PRESETS.get(name or self.default_quality, QUALITY_PRESETS["high"])
        out = dict(q)
        out["name"] = name or self.default_quality
        # Memory guard: never allow analysis larger than the budget can hold.
        if self.memory_budget_gb < 1.0:
            out["analysis_edge"] = min(out["analysis_edge"], 1280)
        return out

    def save(self) -> None:
        self.ensure_dirs()
        payload = asdict(self)
        payload["home"] = str(self.home)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.config_path)

    @classmethod
    def load(cls) -> "Settings":
        home = default_home()
        cfg = home / "config.json"
        if cfg.exists():
            try:
                raw = json.loads(cfg.read_text(encoding="utf-8"))
                raw.pop("home", None)
                known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
                s = cls(home=home, **{k: v for k, v in raw.items() if k in known})
                s.ensure_dirs()
                return s
            except Exception:
                logging.getLogger("kestrel").warning("config corrupt; using defaults")
        s = cls(home=home)
        s.ensure_dirs()
        return s


# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------
_LOG_LOCK = threading.Lock()
_LOG_CONFIGURED = False


class RedactingFilter(logging.Filter):
    """Keep file paths out of shared logs where they could identify a client."""

    _PATH_RE = re.compile(r"(/[^\s:'\"]{2,}/)([^/\s:'\"]+)")

    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - trivial
        try:
            msg = record.getMessage()
            record.msg = self._PATH_RE.sub(r"\1…/\2", msg)
            record.args = ()
        except Exception:
            pass
        return True


def setup_logging(settings: Optional[Settings] = None, level: Optional[str] = None) -> logging.Logger:
    global _LOG_CONFIGURED
    logger = logging.getLogger("kestrel")
    level_name = (level or (settings.log_level if settings else "INFO")).upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    if _LOG_CONFIGURED:
        return logger
    with _LOG_LOCK:
        if _LOG_CONFIGURED:
            return logger
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        logger.addHandler(stream)
        if settings is not None:
            try:
                settings.ensure_dirs()
                fh = logging.FileHandler(settings.log_path, encoding="utf-8")
                fh.setFormatter(fmt)
                fh.addFilter(RedactingFilter())
                logger.addHandler(fh)
            except Exception:
                pass
        logger.propagate = False
        _LOG_CONFIGURED = True
    return logger


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------
def new_id(prefix: str = "") -> str:
    token = uuid.uuid4().hex[:12]
    return f"{prefix}{token}" if prefix else token


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def clamp(v, lo: float, hi: float):
    """Scalar/array clamp. Arrays are clipped element-wise (no truth-value errors)."""
    if hasattr(v, "shape"):
        import numpy as _np
        return _np.clip(v, lo, hi)
    return lo if v < lo else hi if v > hi else v


def human_seconds(seconds: Optional[float]) -> str:
    """Human duration, or a friendly placeholder when there is no estimate yet."""
    if seconds is None:
        return "no estimate"
    seconds = max(0, int(round(float(seconds))))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


_ENGINE: Optional[Any] = None


def engine(settings: Optional[Settings] = None) -> Any:
    """Process-wide engine singleton (imported lazily to avoid circular imports)."""
    global _ENGINE
    if _ENGINE is None:
        from .engine import KestrelEngine
        _ENGINE = KestrelEngine(settings or Settings.load())
    return _ENGINE
