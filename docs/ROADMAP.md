# Roadmap

Honest staging. Anything in *Next* is funded work in progress; anything in *Later* is a candidate, not a promise.
Anything in *Out of scope* has been considered and deliberately rejected.

## Shipped — 1.0 (2026-11-02)

- Analysis-first pipeline with genre, scene, exposure, colour, quality, subject and face analysis.
- Decision engine with 23 built-in profiles and 8 genres, per-photo targets and explanations.
- Linear-light renderer with local masks, detail work, refinement passes and self-measurement.
- Lightroom Classic plug-in: 13 commands, native develop delivery, exact snapshots, compare, revert, sync,
  per-photo batch, background queue, status, diagnostics, settings panel.
- Web workspace: dashboard, library, editor with before/after, batch, profiles, settings, guide.
- Queue with pause/resume/cancel/retry and crash recovery.
- Personalisation: opt-in, bounded, resettable.
- Licence: offline HMAC keys, 14-day trial.
- Test suites: 130 engine/packaging tests, 36 plug-in specification cases.
- Brand system, README, CI, packaging to wheel with the UI inside.

## Next — 1.1

**Detection quality (highest value)**
- Sports evidence cues: banner and advertising-board detection, hoop/backboard, painted key, three-point arc,
  action-face prior. Goal: sports wins on crowd-heavy frames without an override.
- Court coverage: add a texture-variance gate to stop pale studio floors counting as courts.
- Face detection at multiple scales so hero faces are found at the analysis edge.

**Consistency**
- Burst deduplication: near-identical frames share one decision (perceptual hash + timing), so a 20-frame
  celebration does not get 20 different whites.
- Cross-frame uniform colour locking inside a lighting group.

**Delivery**
- Mask delivery as Lightroom-compatible local adjustments where the SDK permits, with a per-mask capability
  report shown before the batch runs.
- A "season" project concept: several collections, one set of profiles and preferences.

## Later — candidate

- Optional ONNX models with documented download and checksum steps (face, segmentation, denoise).
- GPU-accelerated render kernels; the pipeline is already vectorised, so this is throughput, not capability.
- Perspective and horizon correction beyond suggestions.
- Sidecar export in XMP so non-Lightroom editors can read the plan.
- Apple Silicon / CUDA provider auto-selection with a benchmark on first run.
- Localisation of the workspace and plug-in dialogs.

## Out of scope (deliberately)

| Feature | Why not |
|---|---|
| Generative object removal | Not compatible with non-destructive develop delivery or with the "never alter identity" rule; Lightroom and Photoshop already do it well |
| HDR / panorama merge | Belongs before editing; Kestrel edits the merged DNG |
| Cloud processing | The privacy promise is the product; a cloud tier would need a separate threat model |
| Lightroom (cloud) / mobile | Different data model and API; separate product |
| Video | Different pipeline entirely |
| Face reshaping / slimming | Explicitly excluded: retouching must not alter identity |
| Training on customer photographs | Explicitly excluded, architecturally as well as contractually |

## Release gates

Every release must pass:

1. `pytest` — all engine, packaging and profile tests.
2. `python -m tests.lightroom.lua_harness` — 36 plug-in cases against a live engine.
3. `node tests/web/boot.js` — the workspace boots, every view renders, no console errors.
4. Packaging: wheel installs, serves the UI, `kestrel doctor` runs from the installed wheel.
5. Evaluation harness: median score per genre must not regress against the previous release by more than 0.01.
6. README and CHANGELOG updated; capability report reviewed so nothing claims more than it does.
