# Lightroom SDK notes

Research record for the plug-in. Everything here was verified against the Lightroom SDK API reference and the
documented develop-setting keys; where behaviour is undocumented, that is stated and the product avoids relying
on it.

## Runtime constraints

| Constraint | Consequence in the code |
|---|---|
| No `require()` in the plug-in sandbox | `Info.lua` defines `KestrelRequire()`, a `loadfile`-based loader that caches into `package.loaded` |
| Lua 5.1 | no `goto`, no bitwise operators; the JSON codec is written for 5.1 specifically |
| HTTP only inside a task | every `LrHttp` call is inside `startAsyncTask` or `postAsyncTaskWithContext` |
| `LrHttp.post` must run in an async context | all POSTs originate from a task started by a menu command |
| Dialogs must be built on the main thread | options dialogs are built before the task starts |

## HTTP contract

```lua
body, headers = LrHttp.get(url, headersTable, timeout)
body, headers = LrHttp.post(url, postBody, headersTable, method, timeout, totalSize)
```

- `headers` is an array of `{ field = '…', value = '…' }` tables.
- On success, `headers.status` is the integer HTTP status.
- On network failure the call returns `nil` plus an info table with `error.errorCode` (for example
  `cannotConnectToHost`, `timedOut`, `badURL`) and sometimes `partial_data`.
- Without an explicit `Content-Type`, Lightroom sends `text/plain` — the plug-in always sets `application/json`.

`KestrelCore.call()` wraps this: JSON encode/decode, status classification, a retry for GETs, no retry for 4xx, and
a readable message that includes the URL and the start command.

## Develop settings

Written with `photo:applyDevelopSettings(table)` inside `catalog:withWriteAccessDo(name, fn, { timeout = 60 })`.

| Key | Range | Notes |
|---|---|---|
| `Exposure2012` | −5…5 | EV |
| `Contrast2012`, `Highlights2012`, `Shadows2012`, `Whites2012`, `Blacks2012` | −100…100 | |
| `Texture`, `Clarity2012`, `Dehaze` | −100…100 | |
| `Vibrance`, `Saturation` | −100…100 | |
| `Sharpness` | 0…150 | |
| `SharpenRadius` | 0.5…3.0 | |
| `SharpenDetail`, `SharpenEdgeMasking` | 0…100 | |
| `LuminanceSmoothing`, `LuminanceDetail` | 0…100 | |
| `ColorNoiseReduction`, `ColorNoiseReductionDetail`, `…Smoothness` | 0…100 | |
| `PostCropVignetteAmount`, `…Midpoint`, `…Feather`, `…Roundness`, `…HighlightContrast` | as listed | |
| `GrainAmount`, `GrainSize`, `GrainFrequency` | 0…100 | |
| `LensProfileEnable`, `AutoLateralCA` | 0/1 | |
| `LensManualDistortionAmount` | −100…100 | |
| `DefringePurpleAmount`, `DefringeGreenAmount` | 0…20 | |
| `HueAdjustment*`, `SaturationAdjustment*`, `LuminanceAdjustment*` | −100…100 | eight colour ranges each |
| `ToneCurvePV2012`, `ToneCurvePV2012Red/Green/Blue` | point list | |
| `WhiteBalance` | enum | `As Shot`, `Auto`, `Daylight`, `Cloudy`, `Shade`, `Tungsten`, `Fluorescent`, `Flash`, `Custom` |
| `ToneCurveName2012` | enum | `Linear`, `Medium Contrast`, `Strong Contrast`, `Custom` |
| `ProcessVersion` | enum | `5.4`, `6.7`, `11.0`, `15.4` |

Every key the engine emits is filtered and clamped in `KestrelApply.filter()` before it reaches the catalogue. This
matters because Lightroom rejects the **whole table** when one key is unknown, which would otherwise look like a
generic "Lightroom refused the settings" failure. The filter also retries with the strict whitelist if the first
write fails, so an older catalogue degrades instead of breaking.

## Reading the current state

`photo:getDevelopSettings()` returns the current settings as a table. Kestrel stores one snapshot per photograph
on the first edit and uses it for **Compare** and **Revert**. Revert writes that table back, so the restored state
is exactly what was there before, including the process version and camera profile.

## Catalogue access

```lua
local catalog = LrApplication.activeCatalog()
local photos = catalog:getTargetPhotos()          -- selection in Library/Develop
local collection = catalog:getActiveCollection()  -- collection, folder or filmstrip
local photo = catalog:addPhoto(path, { addToSelection = false })
```

`addPhoto` is used only in rendered-TIFF mode: the rendered file is added next to the original, never in place of
it.

## What the SDK cannot do

| Wanted | Reality | Kestrel's answer |
|---|---|---|
| A local adjustment mask, AI or gradient | No documented API creates masks from a plug-in | Global settings are delivered natively; the mask plan is recorded in the packet and baked into the rendered TIFF. Every mask reports `render` capability — nothing claims to be a Lightroom mask |
| Copy local masks between photos | No documented API | Sync-look copies global settings only and says so in the confirmation dialog |
| Invoke Lightroom's AI Denoise | No API | Render denoise in the engine; deliver as a rendered file |
| Read the render preview for an in-dialog comparison | No reliable preview API | Show the reasoning and metrics; the visual comparison happens in the Develop module or the workspace |
| Presets written as `.xmp` with masks | Presets are static; masks need the pixels | Not attempted |

The capability report (`src/kestrel/lightroom/native.py`) encodes this table and is surfaced in three places: the
engine health endpoint, the plug-in status dialog, and the workspace Guide. There is one source of truth, so the
interface cannot drift into claiming a feature that does not exist.

## Error handling rules

- A client error (4xx) is reported immediately with the engine's message; it is never retried.
- A connection error is retried for GETs, then reported with the start command.
- An exception inside a menu command becomes a dialog, never a Lua traceback.
- Per-photo failures are collected and reported with the file name; the batch continues.
- Every failure path is logged with enough detail for support (`kestrel.log` and Lightroom's own log).
