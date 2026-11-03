# Kestrel for Lightroom Classic

The plug-in lives in `Kestrel.lrplugin/` as a single folder. Install it with **File → Plug-in Manager → Add** and
select that folder — full instructions, screenshots and troubleshooting are in the
[main README](../README.md#lightroom-classic-setup).

## Files

| File | Role |
|---|---|
| `Info.lua` | identity, menu declarations, the module loader, `LrInitPlugin` |
| `KestrelCore.lua` | settings, HTTP transport, photo records, import/analysis/preview calls |
| `KestrelJson.lua` | strict JSON encode/decode for Lua 5.1 |
| `KestrelApply.lua` | develop-setting whitelist and clamping, apply, snapshot, revert, compare, sync, TIFF import |
| `KestrelWorkflow.lua` | the per-photo pipeline and its human-readable summaries |
| `KestrelBatch.lua` | engine queue: submit, watch, deliver as results arrive |
| `KestrelDialogs.lua` | options, settings, status, explanation and completion dialogs |
| `KestrelMenus.lua` | the commands behind every menu item, progress scopes and diagnostics |
| `PluginInfoProvider.lua` | Plug-in Manager panel |
| `MenuItem*.lua` | one tiny entry point per menu item |

## Ground rules

- Only documented SDK namespaces are used. No undocumented mask API, no UI automation.
- Every develop key is whitelisted and range-clamped before it is written.
- Every slow operation runs in a background task with a cancellable progress scope.
- Errors are dialogs with a next step, never Lua tracebacks.
- The original photograph is never modified, and the pre-edit settings are snapshotted for exact revert.

## Testing

`tests/lightroom/plugin_spec.lua` runs these files outside Lightroom against a live engine:

```bash
python -m tests.lightroom.lua_harness
```
