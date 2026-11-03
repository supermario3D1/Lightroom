# Architecture

Kestrel is a local engine with two front ends: a FastAPI HTTP service that serves the web workspace, and a
Lightroom plug-in that speaks the same HTTP API from inside Lightroom. Nothing else is required — no daemon, no
cloud, no database server.

```
┌──────────────────────────┐          ┌───────────────────────────┐
│  Lightroom Classic       │          │  Web workspace            │
│  lightroom/*.lrplugin    │          │  web/ (vanilla JS)        │
│  Lua, documented SDK     │          │  served by the engine     │
└───────────┬──────────────┘          └─────────────┬─────────────┘
            │  HTTP 127.0.0.1:8765                  │  same origin
            └───────────────┬───────────────────────┘
                            ▼
                 ┌───────────────────────┐
                 │  FastAPI (api/app.py) │
                 └───────────┬───────────┘
                             ▼
                 ┌───────────────────────┐
                 │  KestrelEngine        │  engine.py — orchestration
                 └───┬───────┬───────┬───┘
      ┌──────────────┘       │       └──────────────┐
      ▼                      ▼                      ▼
┌───────────┐        ┌──────────────┐        ┌──────────────┐
│ import /  │        │  analysis    │        │  decision    │
│ decode    │───────►│  scene, face │───────►│  plan, recipe│
│ imageio   │        │  subject, IQ │        │  explanation │
└───────────┘        └──────┬───────┘        └──────┬───────┘
                            │ masks                 │ plan
                            ▼                       ▼
                     ┌──────────────┐        ┌──────────────┐
                     │ render/masks │───────►│ render/core  │
                     └──────────────┘        │ linear-light │
                                             └──────┬───────┘
                                                    ▼
                                             ┌──────────────┐
                                             │ render/metrics│──► refine (×passes)
                                             └──────┬───────┘
                                                    ▼
                                   ┌────────────────────────────┐
                                   │ lightroom/native  delivery │
                                   │ library (SQLite)  history  │
                                   └────────────────────────────┘
```

## Layers and their contracts

| Layer | Module | Contract |
|---|---|---|
| Import / RAW | `imageio.py` | `decode(path, max_edge)` → `Decoded(rgb float32 0..1, meta)`; read-only, full bit depth where available |
| Analysis | `analysis/*` | `Analyser.analyse(decoded, image_id, analysis_edge)` → `(AnalysisResult, MaskStore)` |
| Decision | `decision/intents.py`, `decision/recipes.py` | `build_plan(analysis, masks, profile, intent, strength, genre_override)` → `EditPlan` |
| Segmentation | `render/masks.py` | named masks with confidence and provenance; never raises on a hard frame |
| Colour science | `color.py`, `analysis/colour.py` | illuminant estimation, cast, skin targets, tone mapping helpers |
| Render | `render/core.py`, `render/metrics.py` | `render(decoded, plan, config)` → image + report; `measure(image, plan, analysis)` → `EditMetrics` |
| Batch | `queueing/*` | `submit`, `submit_collection`, `pause`, `resume`, `cancel`, `retry_failed`, `status` |
| Personalisation | `personalisation/learning.py` | bounded per-genre preference weights; opt-in |
| Profiles | `profiles/library.py` | 23 built-ins, validation, import/export, cloning |
| Storage | `library/db.py` | SQLite: collections, images, versions, profiles, jobs, items, feedback, prefs, audit |
| Delivery | `lightroom/native.py` | develop-setting mapping + capability report |
| API | `api/app.py` | REST + static UI |

## The plan is the product

Everything that decides aesthetics produces a single serialisable object:

```jsonc
{
  "genre": "sports", "intent": "automatic", "strength": 1.0,
  "global": { "exposure": 0.35, "highlights": -45, "shadows": 28, "contrast": 9, "...": 0 },
  "masks": [{ "name": "Subject", "kind": "subject", "opacity": 1.0, "confidence": 0.86,
              "adjustments": { "exposure": 0.35 }, "provenance": "ai-refined" }],
  "explanation": [{ "area": "Subject", "action": "Lifted subject over background",
                    "reason": "Face/subject luminance measured below the genre target …",
                    "amount": "+0.35 EV", "confidence": 0.95 }],
  "metrics": { "score": 0.51, "naturalness": 0.54, "skin_delta_e": 8.2 },
  "warnings": []
}
```

The renderer never invents a decision; the decision layer never touches pixels. That split is what allows:

- the same plan to be rendered locally *or* written as Lightroom develop settings;
- the explanation to be generated from the actual numbers;
- the metrics step to re-measure the rendered result and correct the plan;
- tests to assert decisions without decoding images.

## Render pipeline

1. **Decode** to float32 RGB in 0..1 at a bounded edge (`analysis_edge` scaled by quality).
2. **Tone**: exposure, highlight/shadow recovery, whites/blacks, contrast curve — in linear light with a soft
   shoulder so nothing clips early.
3. **Colour**: temperature/tint (only when the estimate is confident), vibrance then saturation, HSL per range,
   optional split tone.
4. **Local**: masks applied through a guided-filter feather, with a bounding-box limit and a ≥85% area fallback so
   a mask error cannot darken an entire frame.
5. **Detail**: luminance NR (non-local means at High/Maximum), deconvolution at Maximum, unsharp with edge masking.
6. **Finish**: lens distortion/CA correction, vignette, grain, highlight-safety clamp.
7. **Measure** and optionally **refine** (2 passes at High, 3 at Maximum).

Rendering happens at preview resolution for planning and at output resolution for export. The plan is resolution
independent; only the pixel stages scale.

## State and durability

| Data | Where | Guarantee |
|---|---|---|
| Photographs | user's disk | opened read-only, never modified |
| Library index | `KESTREL_HOME/kestrel.db` | SQLite with WAL; `recover_interrupted()` requeues running items on start |
| Versions | DB row + render/thumb files | every render is a new version; nothing overwritten |
| Snapshots | Lightroom preferences (plug-in) | exact pre-edit develop settings per photo |
| Profiles | DB | built-ins immutable, custom editable |
| Feedback | DB | local, opt-in for learning |
| Audit | DB | settings changes, profile changes, licence events |

A crash mid-batch can lose at most the frame being rendered. Completed versions are committed before the next
frame starts.

## Extension points

| Want to change | Do this |
|---|---|
| New genre | cues in `analysis/scene.py`, targets in `decision/recipes.py`, profiles in `profiles/library.py` |
| New quality metric | `render/metrics.py` + `EditMetrics` + the eval harness |
| New model | drop an ONNX file in `models/`, register in `analysis/faces.py` or `render/masks.py`; the capability report picks it up |
| New delivery target | implement the packet writer next to `lightroom/native.py`; the plug-in already consumes the JSON |
| New workspace view | `web/views.js` + nav entry + route; no build step |

## Performance model

Cost is dominated by mask generation and noise reduction, both of which scale with the analysis edge. Quality
presets exist to make that explicit: Standard 1280 px/1 pass, High 1600 px/2, Maximum 2048 px/3 plus deconvolution.
The memory budget caps decode size rather than failing, so a low-RAM machine degrades resolution instead of dying.

## Threading

The HTTP layer is single-process. Rendering runs in a bounded worker pool (`max_workers`), and the queue worker is
a separate thread that takes jobs from SQLite. Analysis is per-image and independent, so parallelism is across
images, never within one — which keeps memory predictable and the plans deterministic.
