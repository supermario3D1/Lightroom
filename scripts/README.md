# Scripts

| Script | What it does | When |
|---|---|---|
| `brand.py` | Generates every brand asset from one geometric definition — mark, icons 32–1024, wordmarks, social card, and the SVG files used by the UI. No external rasteriser needed. | after changing the mark |
| `sync_web.py` | Copies `web/` and `assets/brand/` into `src/kestrel/api/static/` so a wheel install serves the same UI. | after any change in `web/` or the brand assets |

Both are checked by `tests/test_packaging.py` and by CI, which regenerate and fail on a diff.

```bash
python scripts/brand.py
python scripts/sync_web.py
```
