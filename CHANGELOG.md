# Changelog

All notable changes to Kestrel are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

Reliability work found by running the first CI pass on three operating systems.

### Fixed

- Brand pipeline no longer depends on a hard-coded `/usr/share/fonts` path. The wordmark face is resolved across
  Linux, macOS and Windows, `KESTREL_BRAND_FONT_BOLD` / `KESTREL_BRAND_FONT_REGULAR` override it, and
  `python scripts/brand.py --check` verifies the generated vectors, the file manifest and every bitmap's pixel
  dimensions. PNG bytes depend on the Pillow/FreeType/zlib build, so the old byte-for-byte diff could never be a
  portable gate — a fact the new check documents rather than hides.
- Two unreferenced, stale bitmaps (`kestrel-mark-transparent.png`, `kestrel-icon-1024.png`) removed from
  `assets/brand/`; the check now fails on any file that is not in the generator's manifest.
- CI: the Lua syntax check runs under bash on Windows (the job's default PowerShell cannot execute it), and the
  plug-in and packaging jobs publish the tail of a failing command as a check-run annotation through
  `scripts/ci_run.py`, so a red X comes with its reason attached.
- Plug-in harness: engine output goes to a log file instead of a pipe (a pipe nobody drains blocks the child once
  the OS buffer fills — 16 KiB on macOS), each case is printed as it finishes, a run that cannot start reports the
  engine's log tail, and `run_spec(..., on_case=...)` streams progress.

### Changed

- Mask delivery is described the way it actually works. The documented Lightroom SDK has no call that creates a local
  adjustment mask, so every mask in a plan now reports `capability: render` (with `expressible: gradient|ai` for a
  future XMP path) and the capability report says so in plain words. The workspace, the packet dialog and
  `docs/LIGHTROOM_SDK.md` no longer imply that gradients arrive as native Lightroom masks.
- `adaptive` and `native` delivery modes are now genuinely different: `native` sends global develop settings with an
  empty mask document, `adaptive` ships the mask plan for review and re-rendering.
- Plug-in JSON: the encoder escaped text with Lua's `%c` class, which is locale-dependent and on macOS also matches
  the C1 range 0x80-0x9F — the continuation bytes of UTF-8. An em dash in a collection name, a photographer's name or
  a note was silently corrupted on its way to the engine. Escaping is now byte-explicit (C0 controls, quote,
  backslash), which is both correct on every Lua build and cheaper to reason about; the plug-in specification
  asserts UTF-8 and control characters byte for byte.
- Develop settings are clamped to Lightroom's documented ranges inside the engine (`LIMITS` in
  `lightroom/native.py`) as well as in the plug-in, so one corrupt or hand-edited plan cannot make Lightroom reject a
  whole batch's settings table.

### Added

- The workspace boot test now renders a real edit and checks every editor tab: the explanation, the adjustment
  table, the mask list and eight or more measured quality metrics, plus the mask overlay round trip (click a mask,
  see it drawn over the photograph). The quality panel shows an explicit "run an edit" state instead of a table of
  dashes when no version exists.
- `tests/test_lightroom_native.py`: the delivery contract (documented keys, clamped ranges, render-only masks,
  mode differences) is now covered — 137 unit tests.

## [1.0.0] — 2026-11-02

First release. Sports-first, explanation-first, local-only photo finishing for Adobe Lightroom Classic.

### Engine

- Analysis pipeline: genre with confidence, indoor/outdoor, night, stadium and artificial light, mixed lighting,
  backlight, overcast, crowd/grass/court coverage, faces with size filtering, subject mask and bbox, exposure
  percentiles and clipping, dynamic range, white balance and tint with confidence, colour cast strength, noise
  sigma and ISO estimate, sharpness, focus plane, motion blur, lens and camera metadata.
- Decision engine: 23 built-in genre-adaptive profiles, tonal targets per genre, subject separation, skin
  protection with a night venue offset, contrast character (soft/balanced/punchy/editorial), colour character
  (natural/warm/cool/vibrant/editorial).
- Masks: subject, background, face, skin, sky, grass, court, crowd, clothing, object, radial and linear, each with
  a confidence and a delivery capability.
- Renderer: linear-light pipeline, tone and colour stages, local adjustment application, highlight-safety pass,
  sharpening with edge masking, luminance and colour noise reduction, lens corrections, vignette, two-step
  refinement at High and three at Maximum.
- Metrics and self-correction: exposure error, clipping, contrast, saturation, skin ΔE, skin consistency, noise,
  sharpness, haloing, naturalness, subject lift; the plan is corrected when the rendered result drifts.
- Explanations: every stage that moves a value records the action, the reason, the amount and a confidence.
- Crop suggestions, optional perspective correction, and lens/CA assessment.
- Mask review: `GET /api/images/{id}/mask/{kind}` renders any analysis mask over the original with a tinted region
  and an edge outline, and the editor's Local tab opens it in one click.

### Lightroom integration

- Real Lightroom Classic plug-in (Lua, documented SDK only) with 13 menu commands, a settings dialog, an engine
  status report, a diagnostics self-test and a Plug-in Manager panel.
- Native develop settings written inside `withWriteAccessDo`, whitelisted and clamped, with an exact pre-edit
  snapshot per photograph for Revert and Compare.
- Delivery modes: native settings, native only, or a rendered 16-bit TIFF imported beside the original.
- Batch entry points: per-photo loop with cancellable progress, or the engine's background queue with
  apply-on-arrival.
- Sync-look across a selection, with an explicit statement that the SDK cannot copy local masks.

### Workspace and API

- Local FastAPI engine with a bundled, framework-free web workspace: dashboard, library, editor, batch, profiles,
  settings and guide.
- Before/after divider with zoom, style and strength controls, genre override, profile picker, Why/Adjustments/
  Local/Quality tabs, edit history, export, Lightroom packet, accept/reject/rate.
- Profile CRUD with import and export (`*.kestrelprofile.json`).
- Queue with pause, resume, cancel, retry and ETA; interrupted jobs are recovered on restart.
- Settings API, licence activation, live log, hardware and capability report.
- Privacy controls: opt-in personalised learning with visible caps and one-click reset.

### Packaging and fixes

- The bundled sample library now resolves from the committed `assets/samples` folder (a clean clone previously
  found nothing because the only search path was the gitignored `data/samples`). User samples in
  `KESTREL_HOME/samples` take precedence; the legacy path is still searched.
- Hardware detection distinguishes "ONNX Runtime not installed" from "no GPU providers", so `kestrel doctor` and
  the health endpoint state the truth instead of an empty list.

### Quality and tooling

- 130 engine tests and 36 plug-in specification cases, the latter running the real Lua modules against a real
  engine over real HTTP with mock Lightroom namespaces.
- `kestrel doctor`, `kestrel eval` and a batch evaluation harness.
- Brand pipeline: one geometric definition generates the mark, icon set, wordmarks and social card, plus the SVG
  and PNG assets used by the UI.

### Known limits

- Subject masks are classical CV; no segmentation weights ship with the repository.
- AI denoise and deconvolution are rendered, not written as native Lightroom AI settings (no SDK API exists).
- Object removal is not implemented.
- Genre detection can be ambiguous on frames with no dominant subject; a manual override is provided.
- Licence verification is offline and is not DRM.

## [Unreleased]

### Planned

- Optional ONNX segmentation and face models with documented download and checksum steps.
- Perceptual deduplication across a burst so near-identical frames share one decision.
- Native mask delivery if Adobe publishes an API for AI masks.
- Multi-catalogue project linking for studios.
