# Product Requirements Document — Kestrel 1.0

**Status:** shipped
**Owner:** product
**Last updated:** 2026-11-02

---

## 1. Problem

Sports and event photographers deliver hundreds of frames on a deadline from venues that fight the camera:
floodlights with mixed colour temperature, dark crowds, white kit at the clipping point, skin that shifts from
orange to grey depending on which side of the pitch the athlete is standing. Preset packs apply one look to all of
it. The result is either flat, or blown, or plastic — and the photographer cannot explain to a client why.

Existing AI tools optimise for one-click speed. The brief here is the opposite: analyse first, decide per frame,
justify every change, and accept that minutes per photograph are fine if the result is right.

## 2. Goals

| # | Goal | Measure |
|---|---|---|
| G1 | Per-photo decisions, not preset application | every version stores a plan with per-photo values and reasons |
| G2 | Sports excellence | night/indoor match frames score above the no-edit baseline in the evaluation harness; skin ΔE < 10 on primary faces |
| G3 | Honest, explainable edits | 100% of moved settings have an explanation item; no silent editing |
| G4 | Real Lightroom integration | develop settings applied in-catalogue, exact revert, no original modified |
| G5 | Batch at scale | 5000 frames without loss; pause/resume/recover |
| G6 | Local and private | zero network egress from the engine |
| G7 | No fake functionality | every UI control hits a working endpoint; unimplemented features are declared |

**Non-goals for 1.0:** HDR merge, panorama, generative object removal, cloud sync, mobile app, Lightroom (cloud)
integration, video.

## 3. Users

| Persona | Context | Needs |
|---|---|---|
| Club photographer | 300–800 frames per match, delivered same night | fast, consistent, jersey and skin correct, crowd noise tamed |
| Sports agency editor | 50–200 selects from multiple shooters | explainable edits, profile consistency, batch control |
| Wedding photographer | 800–1500 frames, mixed lighting | skin and dress whites protected, consistency across the day |
| Studio / commercial | small sets, colour accuracy | product colour fidelity, predictable output, profiles |
| Enthusiast | few frames at a time | guidance, visible reasoning, no risk to originals |

## 4. Requirements

### 4.1 Analysis (must)

- Genre and confidence, with structural evidence, not keyword matching.
- Scene: indoor/outdoor, night, stadium, artificial, mixed, backlit, overcast, crowd/grass/court coverage.
- Exposure: median, percentiles, clipping both ends, subject and face luminance, dynamic range.
- Colour: temperature, tint, cast strength and confidence, saturation distribution, skin sample statistics.
- Quality: noise sigma, ISO estimate, sharpness, focus plane, motion blur, compression artefacts, lens and camera.
- Subject: mask, bounding box, coverage, confidence; faces filtered by size so crowd faces do not set exposure.

### 4.2 Decision (must)

- Per-genre targets: tonal centre, contrast character, saturation policy, subject separation, skin target,
  noise reduction, sharpening, crop policy.
- Every adjustment traceable to a measurement and a target.
- Strength control that blends toward neutral without changing the *kind* of decision.
- Manual overrides: genre, intent, profile, strength.
- Night/artificial venues: lower skin target and shadow guardrails to avoid the plastic look.

### 4.3 Editing (must)

- Global: exposure, contrast, highlights, shadows, whites, blacks, texture, clarity, dehaze, vibrance, saturation,
  temperature, tint, HSL, tone curve, sharpening with masking, luminance and colour NR, lens corrections, CA,
  defringe, vignette, grain.
- Local: subject, background, face, skin, sky, grass, court, crowd, clothing, object; radial and linear gradients;
  brush geometry.
- Advanced: deconvolution at Maximum, highlight recovery, cast correction, perspective, crop suggestion.
- Never alter identity: no face reshaping, no generative retouching.

### 4.4 Lightroom integration (must)

- Classic 6+; individual, multiple and collection selection.
- Apply develop settings natively, inside write access, whitelisted and range-clamped.
- Preview and explain before committing.
- Compare against the exact pre-edit settings; revert exactly.
- Sync a look across a selection, stating what cannot be synced (masks).
- Background queue that survives Lightroom closing.
- Progress with cancellation; per-frame failure reporting.

### 4.5 Workspace (must)

- Dashboard: connection, library health, queue, recent work, capability.
- Library: grid, selection, import, bulk analyse, bulk edit.
- Editor: large preview, before/after, zoom, strength, analysis detail, explanation, adjustment table, masks,
  quality metrics, history, feedback, export, Lightroom packet.
- Batch: collection pipeline with style/quality/strength, jobs table with controls.
- Profiles: CRUD, import/export.
- Settings: quality, performance, privacy, licence, environment, log.
- Guide: setup, Lightroom installation, implemented vs planned.

### 4.6 Profiles and learning (must)

- Profiles carry intent (subject emphasis, skin protection, contrast and colour character, advanced biases).
- 20+ built-ins across genres; create, rename, duplicate, delete, export, import, apply.
- Feedback: accept, reject, rating, manual adjust.
- Personalisation opt-in, bounded, resettable, visible caps, never trains a shared model.

### 4.7 Batch engine (must)

- 1 to 5000+; per-photo analysis; lighting grouping for consistency.
- Queue with priority, pause, resume, cancel, retry.
- Crash recovery: unfinished items requeued, finished versions retained.
- Progress, ETA from measured throughput, per-item status.
- Memory budget respected; large files decoded at a bounded edge.

### 4.8 Quality assurance (must)

- Evaluation harness scoring exposure, colour, skin, detail, NR, sharpness, haloing, artefacts, naturalness,
  consistency and failures over a collection.
- Regression tests for every decision rule.
- Executable plug-in specification running the real Lua against the real engine.

### 4.9 Commercial (must)

- Brand identity: mark, icon set, wordmarks, social card, consistent palette.
- Onboarding, licence activation, offline verification.
- Logging, versioning, crash reporting via logs.
- Documentation: README with full setup, plus architecture, roadmap, models, datasets, UI, tests.
- Automated tests and CI.

## 5. Acceptance criteria (shipped state)

| Criterion | Result |
|---|---|
| Night football frame beats no-edit baseline on the harness | 0.510 vs 0.499 · *thin margin, tracked* |
| Skin ΔE under 10 on primary faces | 8.2–9.8 on the reference set |
| Every moved develop setting has an explanation | yes, 11 items on the reference frame |
| Revert restores byte-identical settings | verified in the plug-in spec |
| Batch survives restart | verified: `recover_interrupted` requeues running items |
| No network egress | no outbound client in the engine; verified by inspection |
| No placeholder controls | capability report drives the "implemented" table |
| Engine tests | 130 passing (113 engine + 10 packaging + 7 demo) |
| Plug-in specification | 36 cases passing against a live engine |

## 6. Open issues

1. Sports genre still loses to `event` on frames with no dominant face and a large crowd. The override exists;
   detection needs stronger sports evidence cues (banner, hoop, painted key, action-face prior).
2. Pale-floor courts trigger a court-coverage false positive (0.531 on a flat grey frame). Needs a texture/variance
   gate.
3. Face detection is resolution dependent; hero faces are missed at the analysis edge in some crowd frames.
4. The sports-versus-baseline margin (0.510 vs 0.499) is too thin for confidence; it needs the metric band to
   reward subject separation more strongly.

## 7. Success measures after release

- Median frames-per-batch before manual intervention.
- Share of edits accepted without adjustment.
- Re-render rate (a proxy for "the first decision was wrong").
- Support tickets per 100 licences.
- Evaluation harness median score per genre, tracked per release.
