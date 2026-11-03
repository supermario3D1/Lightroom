# UI and UX

## Principles

1. **The photograph is the interface.** The workspace is dark and quiet; the image is the only bright object.
2. **Explain before you impress.** Every result is paired with the reasoning that produced it. A number without a
   reason is noise.
3. **Progressive disclosure.** A beginner sees style, strength and one button. An advanced user opens Adjustments,
   Local, Quality and the profile editor. Nothing is hidden; nothing is forced.
4. **Honest states.** Empty, loading, partial and failed states all say what is happening and what to do next.
   No spinners without text, no fake progress, no disabled button without a reason in its title.
5. **Keyboard first for review.** Compare, accept, reject and re-render never require a mouse.

## Visual system

| Token | Value | Use |
|---|---|---|
| `--bg` | `#0a0c10` | app background |
| `--panel` | `#12161d` | cards and panels |
| `--line` | `#232a36` | hairlines |
| `--text` | `#e8ecf3` | primary text |
| `--text-3` | `#7d8a9c` | secondary text |
| `--accent` | `#e0a458` | primary action, active state, brand |
| `--teal` | `#74c3ac` | links and values inside prose |
| `--ok` / `--warn` / `--danger` | `#75c48d` / `#e2b23c` / `#e2705f` | status |

Type: system sans, 14 px base, 1.5 line height; monospace for identifiers, values and logs. Radii 8/12/18 px. One
elevation level for cards, one for modals. No gradients except on primary buttons and the brand tile.

Accessibility: 4.5:1 minimum contrast on text, visible focus rings on every control, `aria-live` on the view
container, labelled icon buttons, no colour-only status (badges carry text).

## Screen inventory

### Dashboard
Connection chip, engine version, four statistics (photographs, versions, accepted, rejected), the active job with
pause/resume/cancel and ETA, recent imports as a filmstrip, quick actions, and the capability report so a user can
see what the engine will actually deliver.

### Library
Filter by collection and filename, selection with ⌘/Ctrl and shift, bulk analyse, bulk edit. Tiles show genre,
dimensions, status and accepted state. Empty state offers import or the sample gallery.

### Editor
Three columns on desktop:

- **Left** — file facts and the analysis: scene, light, luma, clipping, subject and face luminance, white balance
  with confidence, noise and ISO, sharpness and focus plane, subject coverage, faces, plus any notes the analyser
  wanted to surface.
- **Centre** — the image with a draggable before/after divider, zoom, and the edit history filmstrip.
- **Right** — tabs (*Why*, *Adjustments*, *Local*, *Quality*), then controls (style, strength, quality, genre
  override, profile), then feedback (accept, reject, stars), export and Lightroom delivery.

*Why* lists every change with its reason, amount and confidence, then any warnings. *Adjustments* shows the exact
develop values with a bar per value. *Local* lists masks with confidence and provenance. *Quality* shows measured
metrics with inverted colours where lower is better (halo, noise, ΔE).

### Batch
Collection, genre, style, quality and strength, with a live count and time estimate. Below, the jobs table: status,
progress, failures, ETA and controls. The panel states plainly that pausing and retrying never loses a completed
edit.

### Profiles
Grouped by genre. Each card shows intent values, colour and contrast character, and its provenance (built-in or
custom). Create, edit, duplicate, delete, import, export.

### Settings
Quality and performance, privacy with an explicit consent toggle and the influence caps shown, licence, environment
and delivery capability, and a live log tail for support.

### Guide
The onboarding path, the Lightroom installation steps, the implemented/planned table generated from the engine, and
the honest-limits list.

## Dialogs and feedback

- Modals are single-purpose, keyboard-dismissable and never scroll the page behind them.
- Toasts confirm actions; errors persist longer and quote the engine's own message.
- Destructive actions (delete profile, reset learning, cancel a job) ask once, in plain language, and say what is
  *not* affected.

## Lightroom-side UX

- Long operations always run in a background task with a cancellable progress scope; the UI thread only builds
  dialogs.
- The options dialog remembers the last quality and style, and shows the photograph count and a plain statement
  that originals are untouched.
- The completion dialog lists failures with their reasons, and the reasoning for the first frame, so the user sees
  the thinking without opening the browser.
- Diagnostics produce a report a support person can act on — not a stack trace.

## What we deliberately did not build

- No modal wizard that blocks the workspace.
- No "AI magic" button that hides what it did.
- No dashboards of vanity metrics.
- No dark-pattern licence nagging; the trial ends with a plain statement and edits remain accessible.
