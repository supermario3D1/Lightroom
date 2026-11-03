# Test strategy

Three suites, each answering a different question.

| Suite | Question | Command |
|---|---|---|
| Engine unit tests | do the decisions and the maths behave? | `pytest` |
| Plug-in specification | does the real Lua drive the real engine correctly? | `python -m tests.lightroom.lua_harness` |
| Workspace boot test | does the bundled UI run against the bundled API? | `node tests/web/boot.js` |
| Evaluation harness | is the result actually good? | `kestrel eval <collection>` |

## Engine unit tests (131)

| Area | What is asserted |
|---|---|
| `test_core` | settings persistence, quality presets, hardware detection, error types |
| `test_color` | illuminant estimation on synthetic casts, ΔE2000 reference values, tone mapping monotonicity |
| `test_imageio` | decode paths, bit depth, EXIF, JPEG encode limits |
| `test_analysis` | genre bands with structural gates, night cue, clipping, face size filtering, subject coverage |
| `test_decision` | plan construction per genre, night skin offset, strength blending, explanation coverage |
| `test_render` | mask geometry, local application bounded by bbox, highlight safety, deterministic output |
| `test_metrics` | metric ranges, naturalness composition, halo detection |
| `test_library` | SQLite schema, job recovery, requeue, audit |
| `test_profiles` | 23 built-ins validate, import/export round trip, clone immutability |
| `test_personalisation` | influence caps, consent gate, reset |
| `test_packaging` | UI and brand assets packaged and current, README sections and links, plug-in self-containment |
| `test_demo` | the bundled sample library resolves on a fresh clone, user samples take precedence, empty installs explain where they looked |

Rules:

- A decision gets a test that asserts **the decision**, not a magic number.
- Guardrails get a test with the failing case (pale floor, blank sky, flat grey card).
- No test may depend on network access.

## Plug-in specification (36 cases)

The harness (`tests/lightroom/lua_harness.py`) starts a throw-away engine on a free port, loads the **real**
plug-in files in a Lua 5.1 runtime with mock `Lr*` namespaces, and drives them over real HTTP.

Covered:

- identity and menu wiring (every menu file exists and loads);
- JSON codec round trip including unicode and floats;
- HTTP contract: GET body + `headers.status`, POST round trip, unreachable engine message;
- settings persistence and reset through `LrPrefs`;
- registration idempotence (a photo is imported once and remembered);
- analysis completeness; inspect produces an explanation without touching the catalogue;
- develop-setting filtering: unknown keys dropped, ranges clamped, bad enums rejected;
- apply writes settings and keeps a baseline; revert restores byte-identical values; compare toggles;
- sync copies the look and keeps the target snapshot;
- queue: submit, watch, deliver, failures reported;
- render mode: real 16-bit TIFF written and added to the catalogue, original settings untouched;
- Lightroom rejecting settings is reported, not fatal;
- one unreadable photo does not stop the batch;
- every menu command runs, including cancel paths and an empty selection;
- diagnostics pass against a healthy engine;
- dialogs build their views, settings round trip, info panel builds.

The mock `applyDevelopSettings` **replaces** the settings table, as Lightroom does — a merge-based mock hides a
whole class of bugs.

## Workspace boot test

`tests/web/boot.js` loads the real `index.html` in jsdom, boots the app against a live engine, checks the boot
screen is gone, the engine chip reads `ok`, then visits all seven views and asserts each rendered something and
none reported an error. It also fails on any console error. It is a smoke test: it proves the wiring, not the
pixels.

## Evaluation harness

`kestrel eval <collection>` (or `src/kestrel/eval/harness.py` directly) renders a set and reports:

| Metric | Meaning |
|---|---|
| `median_score` | overall quality across the set |
| `median_exposure_error` | distance from the genre's tonal target |
| `median_skin_delta_e` | skin accuracy on primary faces |
| `set_skin_consistency` | spread of skin results across the set |
| `contrast_spread` | how much the look varies; lower is more coherent |
| `failures` | frames that errored or produced a warning |

Release gate: no regression greater than 0.01 in `median_score` on any genre set.

## Manual QA (before a release)

1. Import 50 mixed frames (RAW + JPEG, three cameras) and run a High batch.
2. Open ten at random in Lightroom; check sliders are editable and sane.
3. Revert one; confirm the catalogue returns to the original develop settings.
4. Compare toggle on five; confirm no flicker of wrong state.
5. Kill the engine mid-batch; restart; confirm the queue resumes and no completed frame is reprocessed.
6. Export one 16-bit TIFF; confirm it opens in Photoshop with the embedded profile.
7. Run Diagnostics in the plug-in; confirm zero failures.

## Known weak spots in coverage

- No real Lightroom is available in CI; the plug-in is exercised through mocks. A manual pass per release is
  mandatory.
- The aesthetic judgement ("does it look good?") is only proxied by metrics. The before/after crops in the PR
  review are the substitute.
- Very large RAW files are not in CI; timing claims come from local measurement.
