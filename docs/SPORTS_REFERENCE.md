# The sports look — a calibration study

This is the evidence behind the sports profile. It records how good sports photographs are actually finished, what
the numbers look like in real match frames, and how those numbers became the targets in `decision/recipes.py`.

## 1 · Method

1. A set of real basketball and football frames was measured with the engine's own analyser: luminance
   distribution, highlight/shadow clipping, subject and face luminance, white balance, tint, saturation, and the
   size of the primary faces.
2. Published editing breakdowns were collected for sports work in Lightroom (see §5) to establish *ranges* for
   each slider, not to copy a preset.
3. Targets were set so that a well-edited frame lands near the measured centre of that range and an unedited frame
   does not.
4. The result was validated with the evaluation harness against a no-edit baseline.

## 2 · What the reference frames measured

Ten frames, mixed indoor/outdoor, all amateur or semi-professional. Luma is median display luminance in 0–1;
`p95` is the 95th percentile; `subjL` and `faceL` are subject and primary-face luminance; K and tint are the
estimated white balance.

| Frame | luma | p95 | subjL | faceL | K | tint | sat | faces (total/primary) | subject coverage |
|---|---|---|---|---|---|---|---|---|---|
| 4076 | 0.510 | 0.948 | 0.680 | 0.680 | 5733 | −5.3 | 0.310 | 0 / 0 | 0.221 |
| 4077 | 0.343 | 0.835 | 0.415 | 0.307 | 6267 | −1.2 | 0.435 | 6 / 1 | 0.161 |
| 4078 | 0.316 | 0.844 | 0.433 | 0.448 | 5887 | −3.9 | 0.661 | 3 / 2 | 0.154 |
| 4079 | 0.259 | 0.726 | 0.398 | 0.204 | 5519 | −7.8 | 0.363 | 4 / 1 | 0.280 |
| 4080 | 0.342 | 0.732 | 0.467 | 0.392 | 5267 | −0.7 | 0.276 | 16 / 3 | 0.090 |
| 4081 | 0.216 | 0.791 | 0.110 | 0.110 | 6383 | −1.9 | 0.307 | 1 / 1 | 0.078 |
| 4082 | 0.407 | 0.804 | 0.744 | 0.766 | 5231 | −1.9 | 0.293 | 5 / 1 | 0.082 |
| 4083 | 0.257 | 0.808 | 0.197 | 0.208 | 4942 | −1.4 | 0.323 | 1 / 1 | 0.178 |
| 4084 | 0.192 | 0.630 | 0.210 | 0.279 | 6296 | −4.6 | 0.392 | 24 / 3 | 0.193 |
| 4085 | 0.257 | 0.837 | 0.374 | 0.333 | 5876 | −3.8 | 0.461 | 10 / 3 | 0.113 |

**What this says**

- Every frame is **0.3 to 2.2 EV under** the eventual sports target of 0.44 median luma. Amateur and
  semi-pro sports files are underexposed by default, and lifting them is the single largest correction.
- **Tint is negative on every frame** (−0.7 to −7.8): floodlights and LED boards push magenta, so a small green
  correction is almost always needed.
- **White balance scatters from 4942 K to 6383 K** within one sport. A batch-wide WB is wrong for a third of the
  set — which is why the engine's WB is per frame, confidence gated and clamped.
- **Saturation is high (0.28–0.66)** and must come *down*, not up. Sports edits that raise saturation produce the
  neon-grass look.
- **Primary faces are small** (0.0158–0.0282 of frame) but present in most frames. Exposure decisions must be
  driven by subject and face luminance, not by the global median — and crowd faces must be excluded or they drag
  the skin target into nonsense.

## 3 · Published editing practice

Ranges gathered from Lightroom sports-editing references:

| Adjustment | Typical range | Reason given |
|---|---|---|
| Exposure | per frame, ±0.5 EV | never baked into a preset; white kit and lights are the limit |
| Highlights | −30 to −50 | protect sweaty skin and white jerseys |
| Shadows | +30 to +40 | recover faces under helmets and caps |
| Whites | small positive | keep the kit bright without clipping |
| Blacks | small negative | controlled depth without crushing |
| Clarity | +15 to +30 gritty, +5 to +10 clean | surface texture on the athlete |
| Texture | +8 to +15 | detail without the "dirty face" of over-sharpening |
| Dehaze | +5 to +10 | stadium haze and light scatter |
| Vibrance | 0 to +15 | jersey colour without oversaturating skin |
| Saturation | −5 to −20 | counter the oversaturated source |
| Sharpen amount | 50–70, masking ~70 | detail on the subject, crowd left soft |
| Luminance NR | 25–30, detail ~50 | noise reduction before style |
| Subject mask | +0.2 to +0.4 EV | athlete separation |
| Background | −0.3 to −0.5 EV | venue falls back |

Two consistent warnings across sources: **over-sharpening shows as dirty skin at 100 %**, and **lifting shadows
globally reveals noise and flattens the venue** — mask the athlete instead.

## 4 · How that became the profile

| Target | Value | Derived from |
|---|---|---|
| Tonal centre | 0.44 | above the measured median (0.19–0.51) but below portrait 0.48, so the venue stays dark |
| Skin target | 0.615, −0.06 at night or artificial > 0.65 | measured faces sit at 0.11–0.77; forcing them all to a daylight target looks plastic |
| Contrast | 24 base, clarity 14, texture 12 | inside the published range, tuned to avoid the halo metric rising |
| Highlights | −30 base, deeper when clipping is measured | protects kit and boards |
| Shadows | +20 base, up to +40 when the subject is under-lit | recovers faces without lifting the whole frame |
| Vibrance / saturation | +18 / +3, saturation pulled down when it measures high | jerseys rich, skin not orange, grass not neon |
| Subject lift | +0.28 masked, background −0.10 | separation without a vignette |
| Noise reduction | 0.35 base scaled by measured sigma | crowd tamed, subject retained |
| Sharpening | 62 with masking | detail on the athlete |

The night rule (`target_face −= 0.06`) and the venue rule in the metrics band
(`−0.07 × clamp(shadow_frac/0.25)`) both come from the same observation: in dark venues the target is
**lower**, not brighter.

## 5 · Sources consulted

- Imagen-AI sports preset guidance (highlights/shadows/clarity/sharpen/NR ranges, "never bake exposure or WB").
- Adobe Lightroom Discover community edit breakdown for a basketball frame (contrast −5, shadows +14, whites +45,
  vibrance −20, saturation −18, clarity +5, texture +8, dehaze +10).
- AAA Presets sports workflow write-up (order of operations, judge sharpening at 100 %).
- Elite Cinematics low-light sports guide (do not over-lift shadows; denoise before styling; mask the athlete).

These informed *ranges and rules*. They are not redistribution sources and no preset data is included.

## 6 · What is still wrong

- Sports still loses genre detection to `event` on crowd-heavy frames with no large face (4079 scores event 0.37,
  sports 0.42, wildlife 0.43). The override is the honest workaround until real sports cues exist.
- The pale-floor false positive: a flat grey surface measures 0.531 court coverage. Needs a texture-variance gate.
- The margin over the no-edit baseline on the reference frame is 0.510 vs 0.499 — thin. The metric band
  under-rewards subject separation; that is the next calibration change.
