# Models and algorithms

Kestrel is a *system* of small, specialised components, not one general model. This document records what runs,
under which licence, and what is deliberately absent.

## What runs today (no external weights)

| Task | Method | Why this and not a network |
|---|---|---|
| Subject mask | Spectral-residual saliency → GrabCut refinement → guided-filter feather | Deterministic, no weights, fast on CPU, and its failure mode is visible (low confidence) rather than confidently wrong |
| Background mask | Complement of subject with a border prior | Trivially correct, and it is the mask that most affects separation |
| Skin mask | YCbCr + Lab chroma gate inside the primary face boxes | Predictable across skin tones when combined with per-frame sampling |
| Face detection | Haar cascades (OpenCV), multi-scale | Available offline, tiny, and good enough for *size* estimation, which is all the exposure logic needs |
| Sky mask | Top-connected luminance/chroma region growing | Stadium roofs and indoor ceilings break global sky tests; connectivity fixes most of it |
| Grass / court | Hue-saturation bands plus texture variance | Direct, explainable, and tunable per venue |
| Crowd | Texture-energy density in the upper/background region | Captures the noise/NR decision more reliably than a detector |
| Exposure analysis | Percentiles, clipping counts, subject/face luminance | Exact, auditable numbers |
| White balance | Grey-world and illuminant estimation with a confidence gate | Refuses to guess when the frame has no neutral reference |
| Noise | Local variance in flat regions, ISO-calibrated | Fast, and the metric matches what NR needs |
| Sharpness | Gradient-energy distributions, focus-plane classification | Resolution independent after normalisation |
| Quality metrics | ΔE2000 skin error, halo energy, naturalness combination | Standard colour science, no training data required |

## Optional, opt-in weights

Drop a compatible ONNX model into `models/` and Kestrel uses it automatically; the health endpoint and
`kestrel doctor` report the active backends so nothing is hidden.

| Slot | Filename pattern | Expected interface | Licence you must respect |
|---|---|---|---|
| Face detection | `models/face*.onnx` | boxes + landmarks, NCHW float input | your model's licence |
| Portrait segmentation | `models/segment*.onnx` | binary person mask, 256–512 px input | your model's licence |
| Denoise | `models/denoise*.onnx` | RGB in/out, same size | your model's licence |

No weights are bundled. Shipping third-party weights would multiply the download, entangle the product licence with
theirs, and — for several popular models — mean redistributing under terms that forbid commercial use. The engine is
built to work well without them and to improve when they are present.

## Deliberately not used

| Component | Reason |
|---|---|
| Closed-weight AI models with commercial restrictions | Cannot be redistributed; cannot be relied on in a paid product |
| Generative models (diffusion, inpainting) | Not non-destructive; conflicts with the identity rule |
| Cloud inference APIs | Violates the local-only promise |
| Face-recognition embeddings | Not needed; identity recognition is out of scope and a privacy liability |
| Trained models on scraped sports photographs | No licence, no consent, no defensible dataset |

## Dataset strategy (see DATASETS.md)

Kestrel's decisions are calibrated on:

1. **User-supplied reference frames** with explicit permission, used only for parameter calibration, never as
   training data.
2. **Synthetic scenes** generated in-repo (gradients, synthetic night sports, flat grey surfaces) for unit tests of
   cues and guardrails.
3. **Public, licensed reference material** for editorial ranges (e.g. published editing breakdowns), which informs
   target *ranges*, not weights.

There is no training loop in the shipped product. "Learning" means bounded preference weights from explicit user
feedback, stored locally.

## Performance envelope

On a 4-core laptop, 24 MP RAW: analysis ≈ 1.2 s, mask generation ≈ 2 s, render ≈ 3 s at High. Noise reduction
dominates at higher quality presets. ONNX providers, when present, are used for inference only; the pixel pipeline
is NumPy/OpenCV and already vectorised.
