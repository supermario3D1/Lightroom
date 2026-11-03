/* Kestrel — editor view: before/after, reasoning, adjustments, masks, quality,
 * delivery and feedback. See app.js for shell and views.js for the rest. */

async function openEditor(id) {
  state.view = "editor";
  if (location.hash !== `#editor/${id}`) location.hash = `#editor/${id}`;
  state.editor = { id, loading: true };
  render();
  try {
    const data = await api(`/api/images/${id}`);
    const versions = data.versions || [];
    state.editor = {
      id,
      image: data.image,
      versions,
      current: versions[0] || null,
      intent: (state.settings || {}).default_intent || "automatic",
      quality: (state.settings || {}).default_quality || "high",
      strength: 1,
      profileId: "",
      genre: "",
      tab: "why",
      compare: 50,
      zoom: 1,
      busy: null,
      error: null,
      elapsed: null,
      overlay: null,
    };
    render();
  } catch (e) {
    toast(e.message, "error");
    state.editor = null;
    navigate("library");
  }
}

async function runEdit(forceAnalysis = false) {
  const ed = state.editor;
  if (!ed || ed.busy) return;
  const t0 = Date.now();
  ed.error = null;
  ed.busy = forceAnalysis ? "Re-analysing the scene, subject and technical quality…"
                          : "Analysing the scene, subject and technical quality…";
  render();
  try {
    if (forceAnalysis || !ed.image.analysis) {
      const res = await api(`/api/images/${ed.id}/analyse?force=${forceAnalysis}`, { method: "POST" });
      ed.image.analysis = res.analysis;
      ed.image.status = res.image.status;
      ed.image.genre = res.image.genre;
      ed.busy = "Building the edit plan…";
      render();
    }
    const payload = await api(`/api/images/${ed.id}/preview`, { method: "POST", body: {
      intent: ed.intent, strength: Number(ed.strength), quality: ed.quality,
      profile_id: ed.profileId || null, genre: ed.genre || null,
      save_version: true, max_edge: 1600,
    }});
    ed.current = payload.version;
    ed.versions = [payload.version, ...(ed.versions || []).filter((v) => v.id !== payload.version.id)];
    if (payload.analysis) ed.image.analysis = payload.analysis;
    if (payload.image) { ed.image.status = payload.image.status; ed.image.genre = payload.image.genre; }
    ed.busy = null;
    ed.elapsed = ((Date.now() - t0) / 1000).toFixed(1);
    ed.compare = 50;
    toast(`Edit ready in ${ed.elapsed}s`);
  } catch (e) {
    ed.busy = null;
    ed.error = e.message;
    toast(e.message, "error");
  }
  render();
}

function viewEditor() {
  const ed = state.editor;
  if (!ed) {
    const picker = h("div", { class: "strip", style: "margin-top:16px" }, h("span", { class: "card-sub" }, "Loading recent…"));
    api("/api/images?limit=12").then((res) => {
      picker.innerHTML = "";
      const images = res.images || [];
      if (!images.length) {
        picker.append(h("span", { class: "card-sub" }, "No photographs imported yet."));
        return;
      }
      for (const img of images) {
        picker.append(h("div", { class: "frame", onclick: () => openEditor(img.id) },
          img.thumb ? h("img", { src: img.thumb, alt: img.filename, loading: "lazy" })
            : h("div", { class: "empty" }, "…"),
          h("span", {}, img.filename)));
      }
    }).catch(() => { picker.innerHTML = ""; });
    return h("div", { class: "card empty" },
      h("h2", {}, "Open a photograph"),
      h("p", {}, "Pick a recent frame below, or choose one in the Library."),
      h("button", { class: "btn btn-primary", onclick: () => navigate("library") }, "Open library"),
      picker);
  }
  if (ed.loading) {
    return h("div", { class: "empty card" }, h("div", { class: "spinner" }), h("p", {}, "Loading…"));
  }

  const img = ed.image || {};
  const a = img.analysis || null;
  const v = ed.current;
  const before = `/api/images/${ed.id}/original?max_edge=1600`;
  const after = v ? `/api/versions/${v.id}/render` : null;

  const overlayUrl = ed.overlay
    ? `/api/images/${ed.id}/mask/${ed.overlay}?max_edge=1600`
    : null;
  const stage = h("div", { class: `canvas-stage ${overlayUrl ? "has-overlay" : ""}`, id: "stage" },
    overlayUrl
      ? h("img", { class: "canvas-overlay", src: overlayUrl, alt: `${ed.overlay} mask overlay`, draggable: "false" })
      : null,
    h("img", { src: before, alt: "Original", draggable: "false", class: "canvas-img",
      style: ed.zoom > 1 ? `transform:scale(${ed.zoom})` : "" }),
    after ? h("div", { class: "after", style: `clip-path:inset(0 0 0 ${ed.compare}%)` },
      h("img", { src: after, alt: "Kestrel edit", draggable: "false", class: "canvas-img",
        style: ed.zoom > 1 ? `transform:scale(${ed.zoom})` : "" })) : null,
    after ? h("div", { class: "canvas-handle", id: "handle", style: `left:${ed.compare}%` },
      h("span", {}, "⇔")) : null);

  const wrap = h("div", { class: "canvas-wrap" },
    stage,
    h("div", { class: "canvas-foot" },
      h("span", { class: "card-sub" }, after
        ? `Drag the divider to compare · ${ed.elapsed ? `rendered in ${ed.elapsed}s` : "rendered"}`
        : "Original — run an edit to see the result"),
      h("div", { class: "spacer" }),
      h("button", { class: "btn btn-sm", onclick: () => { ed.compare = 0; render(); } }, "Before"),
      h("button", { class: "btn btn-sm", onclick: () => { ed.compare = 50; render(); } }, "50/50"),
      h("button", { class: "btn btn-sm", onclick: () => { ed.compare = 100; render(); } }, "After"),
      h("button", { class: "btn btn-sm", onclick: () => { ed.zoom = ed.zoom >= 3 ? 1 : ed.zoom + 1; render(); } },
        `${ed.zoom}×`)));

  const overlayBar = ed.overlay
    ? h("div", { class: "overlay-bar" },
        h("strong", {}, `${ed.overlay} mask`),
        h("span", {}, "— tinted region, white outline. Look for missed edges and false positives before batching."),
        h("div", { class: "spacer" }),
        h("button", { class: "btn btn-sm", onclick: () => { ed.overlay = null; render(); } }, "Back to compare"))
    : null;
  setTimeout(wireCompare, 0);

  const left = h("div", {},
    h("div", { class: "panel" },
      h("div", { class: "row", style: "justify-content:space-between; margin-bottom:6px" },
        h("h2", { title: img.path }, img.filename || "Photograph"),
        h("span", { class: "badge" }, img.status || "new")),
      h("div", { class: "card-sub" },
        [img.width && img.height ? `${img.width}×${img.height}` : null, img.camera, img.lens]
          .filter(Boolean).join(" · ") || "metadata pending"),
      h("div", { class: "row", style: "margin-top:10px" },
        h("button", { class: "btn btn-sm btn-primary", onclick: () => runEdit(false) },
          v ? "Re-render" : "Edit this photo"),
        h("button", { class: "btn btn-sm", onclick: () => runEdit(true) }, "Re-analyse")),
      ed.busy ? h("div", { class: "progress-inline", style: "margin-top:12px" },
        h("div", { class: "spinner" }), h("span", { class: "card-sub" }, ed.busy)) : null,
      ed.error ? h("p", { class: "note danger-note", style: "margin-top:10px" }, ed.error) : null),
    a ? analysisPanel(a) : h("div", { class: "panel" },
      h("p", { class: "card-sub" }, "No analysis yet — press Edit this photo.")));

  const right = h("div", {},
    h("div", { class: "panel" },
      h("div", { class: "panel-tabs" },
        ...[["why", "Why"], ["adjust", "Adjustments"], ["masks", "Local"], ["quality", "Quality"]].map(([t, label]) =>
          h("button", { class: ed.tab === t ? "active" : "", onclick: () => { ed.tab = t; render(); } }, label))),
      ed.tab === "why" ? explainPanel(v) : null,
      ed.tab === "adjust" ? settingsPanel(v) : null,
      ed.tab === "masks" ? masksPanel(v) : null,
      ed.tab === "quality" ? qualityPanel(v) : null),
    h("div", { class: "panel" }, controlsPanel(ed)),
    v ? h("div", { class: "panel" }, feedbackPanel(ed, v)) : null);

  return h("div", { class: "editor" }, left,
    h("div", {}, wrap, overlayBar, v ? versionStrip(ed) : null), right);
}

function analysisPanel(a) {
  const sc = a.scene || {};
  const ex = a.exposure || {};
  const col = a.colour || {};
  const q = a.quality || {};
  const tags = (sc.event_tags || []).slice(0, 6);
  const faces = (a.faces || []).length;
  const primary = faces ? (a.subject_coverage || 0) > 0.03 : false;
  const row = (dt, dd) => [h("dt", {}, dt), h("dd", {}, dd)];
  return h("div", { class: "panel" },
    h("div", { class: "card-head" }, h("h2", {}, "What the engine sees"), h("div", { class: "spacer" }),
      h("span", { class: "badge badge-accent" },
        `${a.genre || "general"} ${(100 * (a.genre_confidence || 0)).toFixed(0)}%`)),
    tags.length ? h("div", { class: "row", style: "margin-bottom:10px" },
      ...tags.map((t) => h("span", { class: "badge" }, t))) : null,
    h("dl", { class: "kv" },
      ...row("Scene", `${sc.indoor > 0.5 ? "indoor" : "outdoor"}${sc.night > 0.4 ? " · night" : ""}${sc.stadium > 0.4 ? " · stadium" : ""}${sc.artificial > 0.5 ? " · artificial" : ""}`),
      ...row("Median luma", fmt.num(ex.median_luma)),
      ...row("Highlight clip", fmt.pct(ex.clipping_high, 2)),
      ...row("Shadow clip", fmt.pct(ex.clipping_low, 2)),
      ...row("Subject luma", `${fmt.num(ex.subject_luma)} · face ${fmt.num(ex.face_luma)}`),
      ...row("Dynamic range", `${fmt.num(ex.dynamic_range_stops, 1)} stops`),
      ...row("White balance", `${Math.round(col.temperature || 0)} K / ${fmt.num(col.tint, 0)}`),
      ...row("Colour cast", `${fmt.pct(col.cast_strength)} (conf ${fmt.pct(col.wb_confidence)})`),
      ...row("Noise σ", `${fmt.num(q.noise_sigma, 3)} · ISO ~${q.iso_estimate || "?"}`),
      ...row("Sharpness", `${fmt.num(q.sharpness)} · focus ${q.focus_plane || "?"}`),
      ...row("Subject", `${fmt.pct(a.subject_coverage, 1)} coverage${primary ? "" : " (wide)"}`),
      ...row("Faces", `${faces} detected`)),
    (a.notes || []).length ? h("div", { style: "margin-top:12px" },
      ...(a.notes || []).slice(0, 5).map((n) => h("p", { class: "note", style: "margin-bottom:6px" }, n))) : null);
}

function explainPanel(v) {
  if (!v) return h("p", { class: "card-sub" }, "Run an edit to see the reasoning behind every change.");
  const items = v.explanation || [];
  return h("div", {},
    h("div", { class: "card-head" }, h("h2", {}, "Why these changes"), h("div", { class: "spacer" }),
      h("span", { class: "badge" }, v.intent || "automatic")),
    items.length
      ? h("div", {}, ...items.map((e) => h("div", { class: "explain-item" },
          h("div", { class: "explain-dot" }),
          h("div", { class: "explain-body" },
            h("strong", {}, e.action || e.area),
            h("p", {}, e.reason || ""),
            h("div", { class: "explain-meta" },
              [e.area, e.amount, e.confidence !== undefined && e.confidence !== null
                ? `confidence ${Math.round(e.confidence * 100)}%` : ""].filter(Boolean).join(" · "))))))
      : h("p", { class: "card-sub" }, "No explanation was recorded for this version."),
    (v.warnings || []).length ? h("div", { style: "margin-top:10px" },
      ...v.warnings.map((w) => h("p", { class: "note warn-note", style: "margin-bottom:6px" }, w))) : null);
}

function settingsPanel(v) {
  if (!v) return h("p", { class: "card-sub" }, "No adjustments yet.");
  const settings = v.settings || [];
  const d = v.denoise || {};
  const sh = v.sharpening || {};
  return h("div", {},
    h("p", { class: "card-sub", style: "margin-bottom:10px" },
      "These are the develop-equivalent values the plug-in writes. They stay editable in Lightroom."),
    settings.length ? h("table", { class: "table compact" }, h("tbody", {}, ...settings.map((s) =>
      h("tr", {}, h("td", {}, s.label),
        h("td", { class: "mono", style: "text-align:right" },
          `${s.display}${s.unit && s.unit !== "K" ? ` ${s.unit}` : s.unit === "K" ? " K" : ""}`),
        h("td", { style: "width:90px" }, s.unit === "K" ? null : h("div", { class: "bar thin" },
          h("i", { class: `fill ${Number(s.value) < 0 ? "negative" : ""}`,
            style: `width:${Math.min(100, Math.abs(Number(s.value)))}%` }))))))) : null,
    h("div", { class: "spacer-y" }),
    h("dl", { class: "kv" },
      h("dt", {}, "Denoise"),
      h("dd", {}, d && Object.keys(d).length
        ? `${d.method || "non-local means"} · strength ${fmt.num(d.strength, 2)}${d.chroma ? ` · chroma ${fmt.num(d.chroma, 2)}` : ""}`
        : "not applied"),
      h("dt", {}, "Sharpening"),
      h("dd", {}, sh && Object.keys(sh).length
        ? `${sh.method || "unsharp"} · radius ${fmt.num(sh.radius, 1)} · amount ${fmt.num(sh.amount, 2)}`
        : "not applied"),
      h("dt", {}, "Crop"),
      h("dd", {}, (v.crop && v.crop.available) ? `suggested ${v.crop.kind}` : "keep original framing"),
      h("dt", {}, "Lens"),
      h("dd", {}, "corrections + chromatic aberration removal enabled")));
}

function masksPanel(v) {
  const ed = state.editor;
  if (!v) return h("p", { class: "card-sub" }, "No local adjustments yet.");
  const masks = v.masks || [];
  if (!masks.length) return h("p", { class: "card-sub" }, "This edit used global adjustments only.");
  return h("div", {},
    h("p", { class: "card-sub", style: "margin-bottom:8px" },
      "Click a mask to see exactly what the engine selected, drawn over the original. " +
      "Confidence is shown so you know what to check before a batch run. These masks shape the rendered deliverable; " +
      "Lightroom's documented SDK cannot create local adjustment masks from a plug-in, so rendered delivery keeps them."),
    ...masks.map((m) => h("button", {
        class: `mask-row ${ed && ed.overlay === m.kind ? "active" : ""}`,
        title: `Show the ${m.kind} mask over the photograph`,
        onclick: () => {
          if (!state.editor) return;
          state.editor.overlay = state.editor.overlay === m.kind ? null : m.kind;
          render();
        },
      },
      h("div", {}, h("div", { class: "queue-name" }, m.name),
        h("div", { class: "queue-meta" },
          m.notes || `${m.kind} mask · ${m.provenance || m.kind_capability || "ai"}`)),
      h("span", { class: "badge" }, `${Math.round((m.confidence || 0) * 100)}%`),
      h("span", { class: "badge badge-accent" }, m.kind))));
}

function qualityPanel(v) {
  if (!v) {
    return h("p", { class: "card-sub" },
      "Run an edit to measure quality — every metric is read from the rendered result, not estimated.");
  }
  const m = v.metrics || {};
  const rows = [
    ["Overall score", m.score, 1, 0],
    ["Naturalness", m.naturalness, 1, 0],
    ["Exposure error", m.exposure_error, 0.25, 2],
    ["Skin ΔE", m.skin_delta_e, 25, 1],
    ["Skin consistency", m.skin_consistency, 12, 1],
    ["Haloing", m.haloing, 1, 2],
    ["Noise level", m.noise_level, 2, 2],
    ["Sharpness", m.sharpness, 1, 2],
    ["Subject lift", m.subject_lift, 0.5, 2],
    ["Highlight clip", m.clipping_high, 0.02, 3],
  ];
  return h("div", {},
    h("p", { class: "card-sub", style: "margin-bottom:10px" },
      "Measured on the rendered result against the genre targets. Warnings are honest, not cosmetic."),
    ...rows.map(([label, value, max, digits]) => {
      const pct = Math.max(0, Math.min(1, Math.abs(Number(value || 0)) / max));
      const invert = ["Exposure error", "Skin ΔE", "Haloing", "Noise level", "Highlight clip"].includes(label);
      return h("div", { class: "metric" },
        h("span", {}, label),
        h("div", { class: "bar" }, h("i", { class: `fill ${invert && pct > 0.45 ? "warn" : ""}`,
          style: `width:${(pct * 100).toFixed(1)}%` })),
        h("span", { class: "mono" }, value === undefined ? "—" : Number(value).toFixed(digits)));
    }),
    (v && v.warnings || []).length ? h("div", { style: "margin-top:10px" },
      ...v.warnings.map((w) => h("p", { class: "note warn-note", style: "margin-bottom:6px" }, w))) : null);
}

function controlsPanel(ed) {
  const intents = ["automatic", "natural", "vibrant", "dramatic", "editorial"];
  const profiles = state.profiles || [];
  const matching = profiles.filter((p) => !ed.genre || p.genre === ed.genre);
  const strengthOut = h("small", {}, `${Math.round(ed.strength * 100)}%`);
  return h("div", {},
    h("div", { class: "label", style: "margin-bottom:6px" }, "Edit style"),
    h("div", { class: "segmented wrap", style: "margin-bottom:12px" },
      ...intents.map((i) => h("button", { class: ed.intent === i ? "active" : "", onclick: () => { ed.intent = i; render(); } },
        intentLabel(i)))),
    h("div", { class: "field" }, h("label", {}, "Strength"), strengthOut,
      h("input", { type: "range", min: "0", max: "1.5", step: "0.05", value: ed.strength,
        oninput: (e) => { ed.strength = Number(e.target.value); strengthOut.textContent = `${Math.round(ed.strength * 100)}%`; } })),
    h("div", { class: "field" }, h("label", {}, "Quality"),
      h("select", { onchange: (e) => { ed.quality = e.target.value; } },
        ...Object.entries((state.health && state.health.quality_presets) || {}).map(([k, p]) =>
          h("option", { value: k, selected: ed.quality === k }, `${p.label} — ~${p.est_seconds}s`)))),
    h("div", { class: "field" }, h("label", {}, "Genre override"),
      h("select", { onchange: (e) => { ed.genre = e.target.value; render(); } },
        h("option", { value: "" }, "Use detected genre"),
        ...["sports", "wedding", "portrait", "event", "landscape", "wildlife", "commercial", "general"]
          .map((g) => h("option", { value: g, selected: ed.genre === g }, g)))),
    h("div", { class: "field" }, h("label", {}, "Profile"),
      h("select", { onchange: (e) => { ed.profileId = e.target.value; } },
        h("option", { value: "" }, "Automatic — genre + scene"),
        ...matching.map((p) => h("option", { value: p.id, selected: ed.profileId === p.id }, p.name)))),
    h("button", { class: "btn btn-primary", style: "width:100%; margin-top:4px", onclick: () => runEdit(false) },
      ed.current ? "Apply and re-render" : "Analyse and edit"),
    h("p", { class: "note", style: "margin-top:8px" },
      "Strength blends every value toward neutral, so 60% is a quieter version of the same decision — not a different look."));
}

function feedbackPanel(ed, v) {
  return h("div", {},
    h("div", { class: "row", style: "margin-bottom:10px" },
      h("strong", {}, "Was this edit right?"), h("div", { class: "spacer" }),
      h("span", { class: "card-sub mono" }, v.id.slice(-6))),
    h("div", { class: "row" },
      h("button", { class: "btn btn-sm", onclick: () => sendFeedback(ed, "accept") }, "✓ Accept"),
      h("button", { class: "btn btn-sm", onclick: () => sendFeedback(ed, "reject") }, "✕ Reject"),
      ...(["1", "2", "3", "4", "5"].map((n) =>
        h("button", { class: "btn btn-sm btn-ghost", title: `Rate ${n} stars`,
          onclick: () => sendFeedback(ed, "rating", Number(n)) }, n)))),
    h("div", { class: "row", style: "margin-top:10px" },
      h("button", { class: "btn btn-sm", onclick: () => exportDialog(ed) }, "Export…"),
      h("button", { class: "btn btn-sm", onclick: () => lightroomDialog(ed) }, "Send to Lightroom"),
      h("a", { class: "btn btn-sm btn-ghost", href: `/api/versions/${v.id}/render`, download: "" }, "Download render")),
    h("p", { class: "card-sub", style: "margin-top:8px" },
      "Feedback stays local and is only used if personalised learning is switched on."));
}

async function sendFeedback(ed, kind, rating = 0) {
  try {
    const v = ed.current;
    const res = await api(`/api/images/${ed.id}/feedback`, { method: "POST", body: {
      kind, version_id: v ? v.id : null, rating,
    }});
    toast(kind === "rating" ? `Rated ${rating}★` : `Marked ${kind}${res.learning_enabled ? "" : " (learning is off)"}`);
    render();
  } catch (e) { toast(e.message, "error"); }
}

function exportDialog(ed) {
  const quality = h("select", {}, h("option", { value: "high" }, "High — 1800px, fast"),
    h("option", { value: "maximum" }, "Maximum — 2200px, deconvolution"),
    h("option", { value: "standard" }, "Standard — quick check"));
  const format = h("select", {}, h("option", { value: "jpeg" }, "JPEG (8-bit, sRGB)"),
    h("option", { value: "tiff" }, "TIFF (16-bit, sRGB)"), h("option", { value: "png" }, "PNG"));
  const out = h("div", { class: "card-sub" }, "");
  openModal("Export", h("div", {},
    h("div", { class: "field" }, h("label", {}, "Quality"), quality),
    h("div", { class: "field" }, h("label", {}, "Format"), format),
    out,
    h("p", { class: "note" }, "Rendering happens locally. A 16-bit TIFF keeps pipeline precision for a round trip into Lightroom or Photoshop.")),
  [h("button", { class: "btn", onclick: closeModal }, "Cancel"),
   h("button", { class: "btn btn-primary", onclick: async (e) => {
     const btn = e.currentTarget;
     btn.disabled = true;
     out.textContent = "Rendering…";
     try {
       const res = await api(`/api/images/${ed.id}/export`, { method: "POST", body: {
         version_id: ed.current ? ed.current.id : null, quality: quality.value, format: format.value,
       }});
       out.textContent = `Written to ${res.path}`;
       out.append(h("div", { style: "margin-top:10px" },
         h("a", { class: "btn btn-sm btn-primary", href: res.download, download: res.filename }, "Download"),
         h("span", { class: "card-sub", style: "margin-left:10px" },
           `${res.filename} · ${fmt.bytes((res.report || {}).bytes || 0)}`)));
       toast("Export complete");
     } catch (err) { out.textContent = err.message; toast(err.message, "error"); }
     btn.disabled = false;
   }}, "Render and export")]);
}

function lightroomDialog(ed) {
  const mode = h("select", {},
    h("option", { value: "adaptive" }, "Adaptive — develop settings + mask plan"),
    h("option", { value: "native" }, "Native — develop settings only"),
    h("option", { value: "render" }, "Rendered — graded copy for re-import"));
  openModal("Send to Lightroom", h("div", {},
    h("p", { class: "card-sub" },
      "This builds the delivery packet the plug-in applies inside Lightroom Classic. Nothing leaves this machine."),
    h("div", { class: "field", style: "margin-top:12px" }, h("label", {}, "Delivery mode"), mode),
    h("p", { class: "note" },
      "Global develop settings are always native. If you need the rendered look (AI denoise, deconvolution), export a 16-bit TIFF " +
      "and import it as a separate file — your RAW stays untouched.")),
  [h("button", { class: "btn", onclick: closeModal }, "Cancel"),
   h("button", { class: "btn btn-primary", onclick: async () => {
     try {
       const res = await api(`/api/images/${ed.id}/apply`, { method: "POST", body: {
         version_id: ed.current ? ed.current.id : null, mode: mode.value,
       }});
       const p = res.payload || {};
       // packet.masks is the versioned mask document ({format, version, masks: [...]}),
       // not a bare array — count the entries either way so the dialog never says "undefined".
       const maskDoc = p.masks || {};
       const maskList = Array.isArray(maskDoc) ? maskDoc : (maskDoc.masks || []);
       openModal("Packet ready", h("div", {},
         h("p", { class: "card-sub" }, `Written to ${p.packet_path}`),
         h("dl", { class: "kv", style: "margin-top:10px" },
           h("dt", {}, "Mode"), h("dd", {}, p.mode || mode.value),
           h("dt", {}, "Develop settings"), h("dd", {}, String(Object.keys(p.settings || {}).length)),
           h("dt", {}, "Local adjustments"), h("dd", {}, String(maskList.length)),
           h("dt", {}, "Explanation items"), h("dd", {}, String((p.explanation || []).length))),
         maskList.length ? h("p", { class: "note", style: "margin-top:10px" },
           "This plan uses masks. They are baked into the rendered deliverable — Lightroom's documented SDK cannot create " +
           "local adjustment masks from a plug-in. Choose rendered delivery to keep them, or native settings only to skip them.") : null,
         h("p", { class: "note", style: "margin-top:10px" },
           "In Lightroom: select the photo → Plug-in Extras → Kestrel → Apply latest edit.")),
       [h("button", { class: "btn btn-primary", onclick: closeModal }, "Done")]);
     } catch (e) { toast(e.message, "error"); }
   }}, "Build packet")]);
}

function versionStrip(ed) {
  const versions = (ed.versions || []).slice(0, 12);
  return h("div", { class: "panel", style: "margin-top:14px" },
    h("div", { class: "row", style: "margin-bottom:10px" },
      h("strong", {}, "Edit history"), h("div", { class: "spacer" }),
      h("span", { class: "card-sub" }, "every render is kept; the original is never overwritten")),
    h("div", { class: "strip" }, ...versions.map((v) => h("div", {
        class: `frame ${ed.current && v.id === ed.current.id ? "active" : ""}`,
        title: `${v.intent} · ${v.quality} · ${fmt.date(v.created)}`,
        onclick: () => { ed.current = v; render(); },
      },
      v.thumb ? h("img", { src: v.thumb, alt: v.id, loading: "lazy" }) : h("div", { class: "empty" }, "—"),
      h("span", {}, `${v.intent} · ${fmt.ago(v.created)}`)))));
}

function wireCompare() {
  const stage = $("#stage");
  const handle = $("#handle");
  if (!stage || !handle) return;
  const after = stage.querySelector(".after");
  let dragging = false;
  const setSplit = (clientX) => {
    const rect = stage.getBoundingClientRect();
    if (!rect.width) return;
    const pct = Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100));
    handle.style.left = `${pct}%`;
    if (after) after.style.clipPath = `inset(0 0 0 ${pct}%)`;
    if (state.editor) state.editor.compare = pct;
  };
  const down = (e) => { dragging = true; setSplit(e.clientX); e.preventDefault(); };
  const move = (e) => { if (dragging) setSplit(e.clientX); };
  const up = () => { dragging = false; };
  handle.addEventListener("pointerdown", down);
  stage.addEventListener("pointerdown", down);
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
}
