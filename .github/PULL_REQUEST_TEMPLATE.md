## What changed

<!-- The user-visible change first. -->

## Why

<!-- The problem this solves, and the measurement or reasoning that supports it. -->

## Evidence

<!-- For visual or decision changes: metrics from `kestrel eval`, before/after crops, or the test that fails before and passes after. -->

```
before:
after:
```

## Checklist

- [ ] `pytest` passes
- [ ] `python -m tests.lightroom.lua_harness` passes (if the plug-in changed)
- [ ] `node tests/web/boot.js` passes (if the workspace changed)
- [ ] `python scripts/sync_web.py` run, if anything in `web/` or `assets/brand/` changed
- [ ] `python scripts/brand.py --check` passes (brand sources, manifest and dimensions)
- [ ] README / CHANGELOG / docs updated where behaviour is described
- [ ] No new dependency without a licence note
- [ ] No placeholder UI, no simulated AI, no invented capability

## What I did not do

<!-- The honest paragraph. Limits, follow-ups, and anything deliberately left out. -->
