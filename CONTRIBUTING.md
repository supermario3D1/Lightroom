<div align="center">

<img src="assets/brand/kestrel-mark.svg" width="64" alt="" />

# Contributing to Kestrel

</div>

Thanks for taking the time. Kestrel is a small, opinionated codebase with a short list of rules — follow them and
your change will fit in without a long review.

---

## Set up

```bash
git clone https://github.com/supermario3D1/Lightroom.git
cd Lightroom
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
kestrel doctor
```

Run the two test suites before you push:

```bash
pytest                                   # engine (113 tests)
python -m tests.lightroom.lua_harness    # plug-in (36 tests, real engine + real HTTP)
```

Both must pass. The plug-in suite starts its own throw-away engine on a free port, so you do not need `kestrel
serve` running.

---

## Non-negotiable rules

These are not style preferences; they are the reasons the product can be trusted.

1. **No fake functionality.** No placeholder buttons, no simulated progress, no invented capability. If a feature
   is not implemented, the UI must say so. A dead button is a bug, not a placeholder.
2. **Decisions stay separate from rendering.** The decision layer produces an `EditPlan`; the renderer consumes it.
   Never put aesthetic judgement inside a pixel operation.
3. **Every adjustment explains itself.** If you add a setting to the plan, add the reason that produced it. The
   explanation list is a product feature, not a debug log.
4. **Filter before you write to Lightroom.** New develop keys go through `KestrelApply.filter` with a range and, if
   it is an enum, its allowed values. The SDK rejects whole tables when one key is unknown.
5. **Never touch an original.** Read-only file opens, no writes outside the Kestrel home directory and the
   catalogue.
6. **No training on user data.** Personalisation may only move the bounded preference weights in
   `personalisation/learning.py`, under the caps in `MAX_INFLUENCE`, and only with consent.
7. **No new runtime dependency without a licence review.** Add it to `docs/MODELS.md` or a note in the PR: name,
   version, licence, and why the standard library or an existing dependency cannot do the job.

---

## Where things live

| Change | File |
|---|---|
| a new genre or scene cue | `src/kestrel/analysis/scene.py` |
| tonal targets, contrast character, per-genre behaviour | `src/kestrel/decision/recipes.py` |
| a new mask | `src/kestrel/render/masks.py` + the mask registry |
| a new quality metric | `src/kestrel/render/metrics.py` and `docs/TEST_STRATEGY.md` |
| Lightroom develop keys | `src/kestrel/lightroom/native.py` + `KestrelApply.lua` ranges |
| a workspace view | `web/views.js` (or `web/editor.js`), then `python scripts/sync_web.py` |
| plug-in behaviour | `lightroom/Kestrel.lrplugin/` — and add a case to `tests/lightroom/plugin_spec.lua` |
| brand assets | `scripts/brand.py` — never hand-edit the generated files; `python scripts/brand.py --check` verifies they are current |

Brand bitmaps are rendered with DejaVu Sans when it is installed (Linux, or `brew install --cask font-dejavu` on
macOS). Point `KESTREL_BRAND_FONT_BOLD` / `KESTREL_BRAND_FONT_REGULAR` at any other face to change the wordmark;
`python scripts/brand.py --check` then verifies the committed vectors, manifest and dimensions, while a plain
`python scripts/brand.py` plus `git diff` is how artwork changes are reviewed.

After changing anything in `web/` or `assets/brand/`, run `python scripts/sync_web.py` so the packaged copy
matches. `tests/test_packaging.py` fails if it does not.

---

## Adding a genre

Genres are data, not branches. To add one (say, motorsport):

1. Add structural cues in `analysis/scene.py` — what evidence does this genre leave in the pixels?
2. Add a `GENRE_BASE` entry in `decision/recipes.py`: tonal target, contrast, saturation, subject lift, noise and
   sharpening policy, skin target if faces matter.
3. Add one or two built-in profiles in `profiles/library.py`.
4. Add the genre name to the API validator, the CLI choices and the workspace select lists.
5. Write a test with a representative image in `tests/`. If the cue is ambiguous, test the *tie* as well.

Do not tune by adding weight to the scoring function until a scene wins. Structural evidence first, weights last.

---

## Code style

- Python 3.10+, type hints on public functions, `from __future__ import annotations`.
- Four-space indent, 110 column soft limit, double quotes, f-strings for messages that are shown to users.
- Dataclasses for data, plain functions for logic, no inheritance trees.
- Docstrings explain *why*, not *what*. The code says what.
- Lua: 4 spaces, `local` everywhere, every SDK call inside a task, every dialog built from real data.
- JavaScript: no framework, no build step, `h()` for elements, no `innerHTML` with user content.
- Errors are shown to the user in plain language with a next step; they are also logged with enough detail for
  support.

---

## Tests

| Kind | Where | What it proves |
|---|---|---|
| unit | `tests/test_*.py` | decisions, colour maths, masks, metrics, profiles, queue |
| plug-in | `tests/lightroom/plugin_spec.lua` | the real plug-in against a real engine over real HTTP |
| evaluation | `kestrel eval <collection>` | quality metrics across a set, against the harness |

A bug fix comes with a test that fails before it and passes after. A new decision comes with a test that shows the
decision, not just the number it produced.

---

## Pull requests

1. One topic per PR. Refactors separate from behaviour changes.
2. Describe the *user-visible* change first, then the implementation.
3. Include before/after evidence for anything visual: metrics from `kestrel eval`, or a crop.
4. State what you did **not** do, and why — that is the most useful paragraph in the PR.
5. CI runs `pytest`, the plug-in suite and a packaging check. All three must be green.

---

## Reporting bugs

Use the bug template. The three things that make a report actionable:

```bash
kestrel doctor
kestrel jobs
```

plus the tail of `<KESTREL_HOME>/kestrel.log`. If it is a Lightroom issue, add the Diagnostics report from
**Plug-in Extras → Kestrel → Diagnostics**.

Please do not attach photographs you do not have the right to share. A crop that shows the problem is enough.

---

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
