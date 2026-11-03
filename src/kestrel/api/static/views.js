/* Kestrel — views: dashboard, library, batch, profiles, settings, guide.
 * Part of the bundled single-page application; see app.js for the shell. */

/* --------------------------------------------------------------- dashboard */
/** Resolve to {data, error} so a failed request is reported, not hidden. */
async function settle(promise, fallback) {
  try {
    return { data: await promise, error: null };
  } catch (error) {
    return { data: fallback, error };
  }
}

async function viewDashboard() {
  const [statsR, jobsR, recentR, healthR] = await Promise.all([
    settle(api("/api/stats"), null),
    settle(api("/api/jobs"), null),
    settle(api("/api/images?limit=8"), { images: [] }),
    settle(api("/api/health"), null),
  ]);
  const stats = statsR.data, rawJobs = jobsR.data, recent = recentR.data, health = healthR.data;
  const problems = [statsR, jobsR, recentR, healthR].filter((result) => result.error);
  if (health) state.health = health;
  state.stats = stats;
  state.jobs = rawJobs;
  const s = stats || {};
  const jobs = (rawJobs && rawJobs.jobs) || [];
  const cards = [
    ["Photographs", s.images ?? 0, `${s.collections ?? 0} collection(s)`],
    ["Edit versions", s.versions ?? 0, "non-destructive, never overwrite"],
    ["Accepted", s.accepted ?? 0, "your decisions are remembered"],
    ["Rejected", s.rejected ?? 0, "used only as preference signal"],
  ];

  const active = jobs.find((j) => ["running", "queued", "paused"].includes(j.status)) || jobs[0];
  const queueCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Processing queue"), h("div", { class: "spacer" }),
      h("button", { class: "btn btn-sm btn-ghost", onclick: () => navigate("batch") }, "Open batch")),
    active ? queueDetail(active) : h("p", { class: "card-sub" },
      "Nothing queued. Import photographs, then start a batch from the Batch view."));

  const recentCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Recently imported"), h("div", { class: "spacer" }),
      h("button", { class: "btn btn-sm btn-ghost", onclick: () => navigate("library") }, "Library")),
    (recent.images || []).length
      ? h("div", { class: "strip" }, ...(recent.images || []).map((img) =>
          h("div", { class: "frame", onclick: () => openEditor(img.id) },
            img.thumb ? h("img", { src: img.thumb, alt: img.filename, loading: "lazy" })
              : h("div", { class: "empty" }, "pending"),
            h("span", {}, img.filename.length > 18 ? `${img.filename.slice(0, 17)}…` : img.filename))))
      : h("p", { class: "card-sub" }, "No photographs yet — import some, or load the sample gallery."));

  const quick = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Start here")),
    h("div", { class: "row" },
      h("button", { class: "btn btn-primary", onclick: importDialog }, "Import photographs"),
      h("button", { class: "btn", onclick: loadDemo }, "Load sample gallery"),
      h("button", { class: "btn btn-ghost", onclick: () => navigate("docs") }, "Lightroom setup")),
    h("p", { class: "card-sub", style: "margin-top:12px" },
      "Sports is the flagship genre. Pick a collection, set Sports, and batch-apply — every frame is still " +
      "analysed and rendered individually."));

  const caps = (state.health && state.health.capabilities) || {};
  const capRows = Object.entries(caps).map(([k, v]) => h("div", { class: "queue-line" },
    h("div", {}, h("div", { class: "queue-name" }, k.replace(/_/g, " "))),
    h("span", { class: `badge ${String(v).startsWith("native") ? "badge-ok" : String(v).startsWith("not") ? "badge-bad" : "badge-warn"}` }, String(v)),
    h("span", { class: "queue-meta" }, "")));

  const problemBanner = problems.length
    ? h("div", { class: "card", style: "margin-bottom:14px" },
        h("h2", {}, "Some dashboard data could not be loaded"),
        h("p", { class: "card-sub" },
          `${problems.length} of 4 requests failed — the cards below show what did arrive. ` +
          "The engine may still be starting; the reason for each failure is listed."),
        ...problems.map((result) => h("p", { class: "note warn-note" },
          String((result.error && result.error.message) || result.error))))
    : null;

  return h("div", {},
    problemBanner,
    h("div", { class: "dash-grid" },
      ...cards.map(([label, value, note]) => h("div", { class: "card" },
        h("div", { class: "stat" },
          h("div", { class: "stat-label" }, label),
          h("div", { class: "stat-value" }, String(value)),
          h("div", { class: "card-sub" }, note))))),
    h("div", { class: "dash-col" },
      h("div", {}, queueCard, h("div", { class: "spacer-y" }), recentCard),
      h("div", {}, quick, h("div", { class: "spacer-y" }),
        h("div", { class: "card" },
          h("div", { class: "card-head" }, h("h2", {}, "Delivery capability")),
          capRows.length ? h("div", {}, ...capRows)
            : h("p", { class: "card-sub" }, "Engine starting…")))));
}

function queueDetail(job) {
  const pct = job.total ? job.completed / job.total : 0;
  const items = (state.jobs && state.jobs.active_job_id === job.id) ? (state.jobs.items || []) : [];
  return h("div", {},
    h("div", { class: "row", style: "margin-bottom:10px" },
      h("span", { class: `badge ${job.status === "running" ? "badge-warn" : job.status === "done" ? "badge-ok" : job.status === "failed" ? "badge-bad" : ""}` }, job.status),
      h("span", { class: "card-sub" }, `${job.kind}${job.message ? ` · ${job.message}` : ""}`),
      h("div", { class: "spacer" }),
      h("button", { class: "btn btn-sm", onclick: () => jobAction(job.id, "pause") }, "Pause"),
      h("button", { class: "btn btn-sm", onclick: () => jobAction(job.id, "resume") }, "Resume"),
      h("button", { class: "btn btn-sm btn-danger", onclick: () => jobAction(job.id, "cancel") }, "Cancel")),
    h("div", { class: "bar" }, h("i", { style: `width:${(pct * 100).toFixed(1)}%` })),
    h("div", { class: "row", style: "margin-top:8px; justify-content:space-between" },
      h("span", { class: "card-sub" }, `${job.completed}/${job.total} · ${job.failed || 0} failed`),
      h("span", { class: "card-sub" }, job.eta_seconds ? `~${fmt.dur(job.eta_seconds)} remaining` : "")),
    items.length ? h("div", { style: "margin-top:10px" },
      ...items.slice(-6).map((it) => h("div", { class: "queue-line" },
        h("div", {}, h("div", { class: "queue-name" }, imageName(it.image_id)),
          h("div", { class: "queue-meta" }, it.error || it.status)),
        h("span", { class: "badge" }, it.status),
        h("span", { class: "queue-meta" }, it.attempts ? `try ${it.attempts}` : "")))) : null);
}

function imageName(id) {
  const img = (state.images || []).find((i) => i.id === id);
  return img ? img.filename : id;
}

async function jobAction(id, action) {
  try {
    await api(`/api/jobs/${id}/${action}`, { method: "POST" });
    toast(`Job ${action === "retry" ? "retried" : action + "d"}`);
    refresh();
  } catch (e) { toast(e.message, "error"); }
}

async function loadDemo() {
  setBusy(true);
  try {
    const res = await api("/api/demo/load", { method: "POST" });
    toast(`${res.created ? res.created.length : res.images || 0} sample photograph(s) added`);
    navigate("library");
  } catch (e) { toast(e.message, "error"); }
  finally { setBusy(false); refresh(); }
}

/* ------------------------------------------------------------------ library */
async function loadCollections() {
  const res = await api("/api/collections");
  state.collections = res.collections || [];
}

async function loadImages() {
  const q = new URLSearchParams();
  if (state.filters.collection) q.set("collection_id", state.filters.collection);
  if (state.filters.status) q.set("status", state.filters.status);
  if (state.filters.search) q.set("search", state.filters.search);
  q.set("limit", "500");
  const res = await api(`/api/images?${q.toString()}`);
  state.images = res.images || [];
  state.imageTotal = res.total || state.images.length;
}

async function viewLibrary() {
  await Promise.all([loadCollections(), loadImages()]);
  const sel = state.selection;
  const counts = {};
  for (const i of state.images) {
    if (i.collection_id) counts[i.collection_id] = (counts[i.collection_id] || 0) + 1;
  }
  const tile = (img) => h("div", {
      class: `tile ${sel.has(img.id) ? "selected" : ""}`,
      title: img.path,
      onclick: (e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey) { toggleSelect(img.id); render(); }
        else openEditor(img.id);
      },
    },
    img.thumb ? h("img", { class: "tile-img", src: img.thumb, alt: img.filename, loading: "lazy" })
      : h("div", { class: "tile-img empty" }, img.status === "new" ? "imported" : "…"),
    h("div", { class: "tile-body" },
      h("div", { class: "tile-name" }, img.filename),
      h("div", { class: "tile-meta" },
        h("span", {}, img.genre || "—"),
        img.width ? h("span", {}, `${img.width}×${img.height}`) : null,
        img.status !== "new" ? h("span", { class: "badge badge-accent" }, img.status) : null,
        img.accepted === 1 ? h("span", { class: "badge badge-ok" }, "accepted") : null)));

  return h("div", {},
    h("div", { class: "lib-toolbar" },
      h("select", { onchange: (e) => { state.filters.collection = e.target.value; render(); } },
        h("option", { value: "" }, "All collections"),
        ...(state.collections || []).map((c) => h("option", { value: c.id, selected: c.id === state.filters.collection },
          `${c.name}${counts[c.id] ? ` (${counts[c.id]})` : ""}`))),
      h("input", { type: "search", placeholder: "Search filename", value: state.filters.search,
        oninput: (e) => { state.filters.search = e.target.value; },
        onchange: () => render(), style: "max-width:220px" }),
      h("div", { class: "spacer" }),
      h("span", { class: "card-sub" }, `${state.images.length} of ${state.imageTotal ?? state.images.length}`),
      h("button", { class: "btn btn-sm", onclick: newCollectionDialog }, "New collection"),
      h("button", { class: "btn btn-sm", onclick: importDialog }, "Import"),
      h("button", { class: "btn btn-sm", onclick: selectAll },
        sel.size === state.images.length && sel.size ? "Clear selection" : "Select all"),
      h("button", { class: "btn btn-sm", disabled: !sel.size, onclick: analyseSelected }, `Analyse (${sel.size})`),
      h("button", { class: "btn btn-sm btn-primary", disabled: !sel.size, onclick: batchSelected },
        sel.size ? `Edit ${sel.size}` : "Edit selected")),
    state.images.length
      ? h("div", { class: "grid-images" }, ...state.images.map(tile))
      : h("div", { class: "empty card" },
          h("h2", {}, "No photographs yet"),
          h("p", {}, "Import files or folders from this machine, or load the sample gallery to explore the engine."),
          h("div", { class: "row" },
            h("button", { class: "btn btn-primary", onclick: importDialog }, "Import photographs"),
            h("button", { class: "btn", onclick: loadDemo }, "Load sample gallery"))));
}

function toggleSelect(id) {
  if (state.selection.has(id)) state.selection.delete(id);
  else state.selection.add(id);
}
function selectAll() {
  if (state.selection.size === state.images.length) state.selection.clear();
  else state.images.forEach((i) => state.selection.add(i.id));
  render();
}

async function analyseSelected() {
  setBusy(true);
  let done = 0;
  for (const id of Array.from(state.selection)) {
    try { await api(`/api/images/${id}/analyse`, { method: "POST" }); }
    catch (e) { toast(e.message, "error"); }
    done += 1;
    if (done % 3 === 0) toast(`Analysed ${done}/${state.selection.size}`);
  }
  setBusy(false);
  toast(`Analysis complete for ${done} photograph(s)`);
  refresh();
}

async function batchSelected() {
  try {
    const res = await api("/api/jobs", { method: "POST", body: {
      image_ids: Array.from(state.selection), kind: "edit", priority: 6,
      options: { intent: "automatic", quality: (state.settings || {}).default_quality || "high", strength: 1.0 },
    }});
    toast(`Queued ${(res.job || {}).total || state.selection.size} photograph(s)`);
    navigate("batch");
  } catch (e) { toast(e.message, "error"); }
}

function newCollectionDialog() {
  const name = h("input", { type: "text", placeholder: "e.g. 2026 Season — Game 3" });
  const hint = h("select", {},
    ...["", "sports", "wedding", "portrait", "event", "landscape", "wildlife", "commercial"]
      .map((g) => h("option", { value: g }, g || "No genre hint")));
  openModal("New collection", h("div", {},
    h("div", { class: "field" }, h("label", {}, "Name"), name),
    h("div", { class: "field" }, h("label", {}, "Genre hint"), hint),
    h("p", { class: "note" }, "The hint biases detection for the whole collection — useful when a stadium ceiling fools the classifier.")),
  [h("button", { class: "btn", onclick: closeModal }, "Cancel"),
   h("button", { class: "btn btn-primary", onclick: async () => {
     if (!name.value.trim()) return toast("Name a collection first", "error");
     try {
       await api("/api/collections", { method: "POST", body: { name: name.value.trim(), genre_hint: hint.value } });
       closeModal(); toast("Collection created"); refresh();
     } catch (e) { toast(e.message, "error"); }
   }}, "Create")]);
}

async function importDialog() {
  const paths = h("textarea", { rows: 4, placeholder: "/photos/2025-06-14 Lakers\n/Photos/Game 3/…" });
  const file = h("input", { type: "file", multiple: true, accept: "image/*,.dng,.cr2,.cr3,.nef,.arw,.raf,.orf,.rw2" });
  const coll = h("select", {}, h("option", { value: "" }, "No collection"),
    ...(state.collections || []).map((c) => h("option", { value: c.id }, c.name)));
  openModal("Import photographs", h("div", {},
    h("div", { class: "field" }, h("label", {}, "Folders or files (one path per line)"), paths),
    h("div", { class: "field" }, h("label", {}, "…or upload from this computer"), file),
    h("div", { class: "field" }, h("label", {}, "Collection"), coll),
    h("p", { class: "note" }, "Files are read from disk and registered in the local library. Originals are never modified.")),
  [h("button", { class: "btn", onclick: closeModal }, "Cancel"),
   h("button", { class: "btn btn-primary", onclick: async () => {
     const list = paths.value.split(/\n+/).map((s) => s.trim()).filter(Boolean);
     try {
       let res;
       if (list.length) {
         res = await api("/api/import", { method: "POST", body: { paths: list, collection_id: coll.value || null } });
       } else if (file.files.length) {
         const fd = new FormData();
         Array.from(file.files).forEach((f) => fd.append("files", f));
         const qs = coll.value ? `?collection_id=${encodeURIComponent(coll.value)}` : "";
         res = await api(`/api/import/upload${qs}`, { method: "POST", body: fd });
       } else return toast("Choose files or paste a path", "error");
       toast(`Imported ${res.created.length} · skipped ${res.skipped.length} · failed ${res.failed.length}`);
       if (res.failed.length) {
         openModal("Some files could not be imported", h("div", {}, h("p", { class: "card-sub" }, "The rest were imported. These failed:"),
           h("div", { class: "mono", style: "margin-top:10px; color:var(--danger)" }, res.failed.map((f) => `${f.path} — ${f.error}`).join("\n"))),
         [h("button", { class: "btn btn-primary", onclick: closeModal }, "OK")]);
       } else closeModal();
       refresh();
     } catch (e) { toast(e.message, "error"); }
   }}, "Import")]);
}

/* -------------------------------------------------------------------- batch */
async function viewBatch() {
  await Promise.all([loadCollections(), loadImages(), loadProfiles()]);
  const jobs = await api("/api/jobs");
  state.jobs = jobs;
  const opts = state.batch || (state.batch = {
    collection: "", intent: "automatic", quality: (state.settings || {}).default_quality || "high",
    strength: 1, genre: "",
  });
  const coll = state.collections.find((c) => c.id === opts.collection);
  const count = coll ? state.images.filter((i) => i.collection_id === coll.id).length : state.images.length;
  const preset = ((state.health || {}).quality_presets || {})[opts.quality] || { est_seconds: 12 };
  const est = count * preset.est_seconds;

  const form = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "New batch"), h("div", { class: "spacer" }),
      h("span", { class: "card-sub" }, `${count} photograph(s) · ~${fmt.dur(est)} at ${preset.label || opts.quality}`)),
    h("div", { class: "row", style: "align-items:flex-end" },
      h("div", { class: "field", style: "flex:2; min-width:200px" }, h("label", {}, "Collection"),
        h("select", { onchange: (e) => { opts.collection = e.target.value; render(); } },
          h("option", { value: "" }, "Everything in the library"),
          ...state.collections.map((c) => h("option", { value: c.id, selected: opts.collection === c.id }, c.name)))),
      h("div", { class: "field" }, h("label", {}, "Genre"),
        h("select", { onchange: (e) => { opts.genre = e.target.value; } },
          h("option", { value: "" }, "Auto-detect"),
          ...["sports", "wedding", "portrait", "event", "landscape", "wildlife", "commercial"].map((g) =>
            h("option", { value: g, selected: opts.genre === g }, g)))),
      h("div", { class: "field" }, h("label", {}, "Style"),
        h("select", { onchange: (e) => { opts.intent = e.target.value; } },
          ...["automatic", "natural", "vibrant", "dramatic", "editorial"].map((i) =>
            h("option", { value: i, selected: opts.intent === i }, intentLabel(i))))),
      h("div", { class: "field" }, h("label", {}, "Quality"),
        h("select", { onchange: (e) => { opts.quality = e.target.value; render(); } },
          ...Object.entries((state.health || {}).quality_presets || {}).map(([k, p]) =>
            h("option", { value: k, selected: opts.quality === k }, p.label)))),
      h("div", { class: "field", style: "min-width:150px" }, h("label", {}, `Strength ${Math.round(opts.strength * 100)}%`),
        h("input", { type: "range", min: "0.2", max: "1.5", step: "0.05", value: opts.strength,
          oninput: (e) => { opts.strength = Number(e.target.value); e.target.previousSibling.textContent = `Strength ${Math.round(opts.strength * 100)}%`; } })),
      h("button", { class: "btn btn-primary", onclick: startBatch }, "Start batch")),
    h("p", { class: "card-sub", style: "margin-top:10px" },
      "Lighting-consistent groups share a grading model, but each frame still gets its own exposure, subject lift and noise reduction. " +
      "Jobs survive a restart: unfinished items are requeued, finished edits are never lost."));

  const jobRows = (jobs.jobs || []).map((j) => h("tr", {},
    h("td", {}, h("div", {}, j.kind), h("small", { class: "mono" }, j.id)),
    h("td", {}, h("span", { class: `badge ${j.status === "done" ? "badge-ok" : j.status === "failed" ? "badge-bad" : j.status === "running" ? "badge-warn" : ""}` }, j.status)),
    h("td", {}, h("div", { class: "bar" }, h("i", { style: `width:${((j.completed / Math.max(j.total, 1)) * 100).toFixed(1)}%` })),
      h("small", {}, `${j.completed}/${j.total}`)),
    h("td", {}, String(j.failed || 0)),
    h("td", {}, j.eta_seconds ? fmt.dur(j.eta_seconds) : "—"),
    h("td", {}, fmt.ago(j.created)),
    h("td", {}, h("div", { class: "row" },
      h("button", { class: "btn btn-sm", onclick: () => jobAction(j.id, "pause") }, "Pause"),
      h("button", { class: "btn btn-sm", onclick: () => jobAction(j.id, "resume") }, "Resume"),
      h("button", { class: "btn btn-sm", onclick: () => jobAction(j.id, "retry") }, "Retry"),
      h("button", { class: "btn btn-sm btn-danger", onclick: () => jobAction(j.id, "cancel") }, "Cancel")))));

  return h("div", {},
    form,
    h("div", { class: "spacer-y" }),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Jobs"), h("div", { class: "spacer" }),
        h("span", { class: "card-sub" }, "pause, resume and retry are always available")),
      (jobs.jobs || []).length ? h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", {}, "Job"), h("th", {}, "Status"), h("th", {}, "Progress"),
          h("th", {}, "Failed"), h("th", {}, "ETA"), h("th", {}, "Created"), h("th", {}, ""))),
        h("tbody", {}, ...jobRows))
        : h("p", { class: "card-sub" }, "No jobs yet.")));
}

async function startBatch() {
  const opts = state.batch;
  const options = { intent: opts.intent, quality: opts.quality, strength: opts.strength };
  if (opts.genre) options.genre = opts.genre;
  try {
    let body;
    if (opts.collection) {
      body = { collection_id: opts.collection, kind: "edit", priority: 6, options };
    } else {
      const ids = state.images.map((i) => i.id);
      if (!ids.length) return toast("Import photographs first", "error");
      body = { image_ids: ids, kind: "edit", priority: 6, options };
    }
    const res = await api("/api/jobs", { method: "POST", body });
    toast(`Queued ${(res.job || {}).total || ""} photograph(s)`);
    refresh();
  } catch (e) { toast(e.message, "error"); }
}

/* ----------------------------------------------------------------- profiles */
async function loadProfiles() {
  const res = await api("/api/profiles");
  state.profiles = res.profiles || [];
}

async function viewProfiles() {
  await loadProfiles();
  const genres = Array.from(new Set(state.profiles.map((p) => p.genre)));

  const profileCard = (p) => {
    const head = h("div", { class: "row" },
      h("h3", {}, p.name),
      h("div", { class: "spacer" }),
      p.builtin ? h("span", { class: "badge" }, "built-in")
                : h("span", { class: "badge badge-accent" }, "custom"));
    const swatches = h("div", { class: "swatches" },
      h("span", { class: "swatch", title: p.colour_character,
        style: `background:${swatchColour(p.colour_character)}` }),
      h("span", { class: "swatch-text" }, `${p.contrast_character} · ${p.colour_character}`));
    const facts = h("dl", { class: "kv", style: "margin-top:6px" },
      h("dt", {}, "Subject"), h("dd", {}, fmt.num(p.subject_emphasis, 2)),
      h("dt", {}, "Skin"), h("dd", {}, fmt.num(p.skin_protection, 2)),
      h("dt", {}, "Detail"), h("dd", {}, fmt.num(p.detail_preservation, 2)));
    const actions = h("div", { class: "row", style: "margin-top:8px" },
      h("button", { class: "btn btn-sm", onclick: () => editProfile(p) },
        p.builtin ? "Duplicate" : "Edit"),
      p.builtin ? null : h("button", { class: "btn btn-sm btn-ghost",
        onclick: () => deleteProfile(p) }, "Delete"),
      h("a", { class: "btn btn-sm btn-ghost", href: `/api/profiles/${p.id}/export`, download: "" }, "Export"));
    return h("div", { class: "card profile-card" }, head,
      h("div", { class: "card-sub" }, p.description || ""), swatches, facts, actions);
  };

  const sections = genres.map((g) => h("div", { style: "margin-bottom:18px" },
    h("div", { class: "label", style: "margin-bottom:8px" }, g),
    h("div", { class: "profile-grid" },
      ...state.profiles.filter((p) => p.genre === g).map(profileCard))));

  return h("div", {},
    h("div", { class: "lib-toolbar" },
      h("span", { class: "card-sub" }, `${state.profiles.length} profiles`),
      h("div", { class: "spacer" }),
      h("button", { class: "btn btn-sm", onclick: importProfile }, "Import"),
      h("button", { class: "btn btn-sm btn-primary", onclick: () => editProfile(null) }, "New profile")),
    h("p", { class: "card-sub", style: "margin-bottom:14px" },
      "Profiles describe intent — subject separation, contrast character, skin protection, colour bias — not a fixed set of sliders. " +
      "The decision engine translates a profile into settings for each photograph."),
    ...sections);
}

function swatchColour(c) {
  return { warm: "#e2a55c", cool: "#6fa8d6", vibrant: "#8fce8a", editorial: "#b9a7d6" }[c] || "#9aa4b2";
}

function editProfile(profile) {
  const draft = profile
    ? JSON.parse(JSON.stringify(profile))
    : { name: "", genre: "sports", description: "", subject_emphasis: 0.7, skin_protection: 0.85,
        contrast_character: "balanced", colour_character: "natural", saturation_bias: 0,
        vibrance_bias: 0, detail_preservation: 0.7, noise_reduction_bias: 0.5 };
  const textField = (label, key) => h("div", { class: "field" }, h("label", {}, label),
    h("input", { type: "text", value: draft[key] || "", oninput: (e) => { draft[key] = e.target.value; } }));
  const rangeField = (label, key, min, max, step) => {
    const out = h("small", {}, String(draft[key]));
    return h("div", { class: "field" }, h("label", {}, label),
      h("input", { type: "range", min, max, step, value: draft[key],
        oninput: (e) => { draft[key] = Number(e.target.value); out.textContent = String(draft[key]); } }), out);
  };
  const selectField = (label, key, options) => h("div", { class: "field" }, h("label", {}, label),
    h("select", { onchange: (e) => { draft[key] = e.target.value; } },
      ...options.map((o) => h("option", { value: o, selected: draft[key] === o }, o))));

  openModal(profile ? (profile.builtin ? "Duplicate profile" : "Edit profile") : "New profile", h("div", {},
    h("div", { class: "row" },
      h("div", { style: "flex:2; min-width:180px" }, textField("Name", "name")),
      h("div", { style: "flex:1" }, selectField("Genre", "genre",
        ["sports", "wedding", "portrait", "event", "landscape", "wildlife", "commercial", "general"]))),
    textField("Description", "description"),
    h("div", { class: "row" },
      h("div", { style: "flex:1" }, rangeField("Subject emphasis", "subject_emphasis", 0, 1.2, 0.05)),
      h("div", { style: "flex:1" }, rangeField("Skin protection", "skin_protection", 0, 1, 0.05)),
      h("div", { style: "flex:1" }, rangeField("Detail preservation", "detail_preservation", 0, 1, 0.05))),
    h("div", { class: "row" },
      h("div", { style: "flex:1" }, selectField("Contrast character", "contrast_character", ["soft", "balanced", "punchy", "editorial"])),
      h("div", { style: "flex:1" }, selectField("Colour character", "colour_character", ["natural", "warm", "cool", "vibrant", "editorial"]))),
    h("p", { class: "note" },
      "Advanced preferences (white-balance tolerance, grass boost, crowd control, HSL nudges) travel with the profile and can be edited in the exported JSON.")),
  [h("button", { class: "btn", onclick: closeModal }, "Cancel"),
   h("button", { class: "btn btn-primary", onclick: async () => {
     if (!draft.name.trim()) return toast("Give the profile a name", "error");
     try {
       if (profile && !profile.builtin) {
         await api(`/api/profiles/${profile.id}`, { method: "PATCH", body: { profile: draft } });
       } else if (profile) {
         draft.name = `${profile.name} copy`;
         draft.builtin = false;
         draft.id = "";
         await api("/api/profiles", { method: "POST", body: { profile: draft } });
       } else {
         await api("/api/profiles", { method: "POST", body: { profile: draft } });
       }
       closeModal(); toast("Profile saved"); refresh();
     } catch (e) { toast(e.message, "error"); }
   }}, "Save")]);
}

async function deleteProfile(p) {
  if (!confirm(`Delete “${p.name}”? Photographs already edited keep their versions.`)) return;
  try { await api(`/api/profiles/${p.id}`, { method: "DELETE" }); toast("Deleted"); refresh(); }
  catch (e) { toast(e.message, "error"); }
}

function importProfile() {
  const ta = h("textarea", { rows: 8, placeholder: "Paste a .kestrelprofile.json document" });
  openModal("Import profile", h("div", {}, h("div", { class: "field" }, h("label", {}, "Profile JSON"), ta)),
    [h("button", { class: "btn", onclick: closeModal }, "Cancel"),
     h("button", { class: "btn btn-primary", onclick: async () => {
       try {
         const doc = JSON.parse(ta.value);
         await api("/api/profiles/import", { method: "POST", body: { document: doc } });
         toast("Profile imported"); closeModal(); refresh();
       } catch (e) { toast(e.message, "error"); }
     }}, "Import")]);
}

/* ----------------------------------------------------------------- settings */
async function viewSettings() {
  const [data, personal, licence, logs] = await Promise.all([
    api("/api/settings"), api("/api/personalisation"), api("/api/licence"),
    api("/api/logs?tail=60").catch(() => ({ lines: [] })),
  ]);
  state.settings = data.settings;
  state.personalisation = personal;
  state.licence = licence;
  const s = data.settings;
  const num = (label, key, min, max, step = 1) => h("div", { class: "field" },
    h("label", {}, label),
    h("input", { type: "number", value: s[key], min, max, step, onchange: (e) => saveSetting(key, Number(e.target.value)) }));
  const sel = (label, key, options) => h("div", { class: "field" }, h("label", {}, label),
    h("select", { onchange: (e) => saveSetting(key, e.target.value) },
      ...options.map(([v, l]) => h("option", { value: v, selected: String(s[key]) === String(v) }, l))));
  const learned = Object.keys(personal.genres || {});
  const caps = (state.health && state.health.capabilities) || {};
  const limits = (state.health && state.health.limits) || {};

  return h("div", { class: "dash-col" },
    h("div", {},
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Quality and performance")),
        h("div", { class: "row" },
          sel("Default quality", "default_quality", Object.entries(s.quality_presets || {}).map(([k, p]) => [k, `${p.label} — ${p.analysis_edge}px`])),
          sel("Default style", "default_intent", [["automatic", "Auto"], ["natural", "Natural"], ["vibrant", "Vibrant"], ["dramatic", "Dramatic"], ["editorial", "Editorial"]])),
        h("div", { class: "row" },
          num("Workers", "max_workers", 1, 4), num("Memory budget (GB)", "memory_budget_gb", 0.6, 64, 0.5)),
        h("p", { class: "card-sub" },
          "Higher quality means more analysis resolution, more refinement passes and slower per-photo processing. " +
          "The brief asks for quality over speed, so High is the default.")),
      h("div", { class: "spacer-y" }),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Privacy")),
        h("p", { class: "card-sub" },
          "All processing is local. Photographs are never uploaded, and no shared model is trained on your files. " +
          "Optional personalised learning only adjusts bounded preference numbers from your own explicit feedback."),
        h("div", { class: "row", style: "margin-top:10px" },
          h("button", { class: "btn", onclick: () => setConsent(!personal.enabled) },
            personal.enabled ? "Turn personalised learning off" : "Turn personalised learning on"),
          h("button", { class: "btn btn-ghost", onclick: resetPersonalisation }, "Reset learned preferences")),
        h("dl", { class: "kv", style: "margin-top:12px" },
          h("dt", {}, "Consent"), h("dd", {}, personal.enabled ? "on" : "off"),
          h("dt", {}, "Genres learned"), h("dd", {}, learned.length ? learned.join(", ") : "none yet"),
          h("dt", {}, "Feedback events"), h("dd", {}, String(personal.feedback_events ?? 0)),
          h("dt", {}, "Cloud processing"), h("dd", {}, s.allow_cloud ? "enabled" : "never")),
        h("p", { class: "note", style: "margin-top:10px" }, personal.consent || "")),
      h("div", { class: "spacer-y" }),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Licence")),
        h("dl", { class: "kv" },
          h("dt", {}, "Status"), h("dd", {}, licence.status),
          h("dt", {}, "Tier"), h("dd", {}, licence.tier || "trial"),
          h("dt", {}, "Trial days left"), h("dd", {}, String(licence.trial_days_remaining ?? "—"))),
        h("div", { class: "row", style: "margin-top:10px" },
          h("input", { type: "text", id: "licence-key", placeholder: "Paste licence key", style: "flex:1" }),
          h("button", { class: "btn", onclick: activateLicence }, "Activate")),
        licence.message ? h("p", { class: "note", style: "margin-top:10px" }, licence.message) : null)),
    h("div", {},
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Environment")),
        h("dl", { class: "kv" },
          h("dt", {}, "Home"), h("dd", { class: "mono" }, s.home),
          h("dt", {}, "Version"), h("dd", {}, (state.health || {}).version || ""),
          h("dt", {}, "Python"), h("dd", {}, (state.health || {}).python || ""),
          h("dt", {}, "Platform"), h("dd", {}, (state.health || {}).platform || ""),
          h("dt", {}, "CPUs"), h("dd", {}, String(((state.health || {}).hardware || {}).cpu_count || "?"))),
        h("div", { class: "row", style: "margin-top:12px" },
          h("button", { class: "btn btn-sm", onclick: () => navigate("docs") }, "Setup guide"),
          h("button", { class: "btn btn-sm btn-ghost", onclick: () => refresh() }, "Re-check"))),
      h("div", { class: "spacer-y" }),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Delivery support")),
        h("table", { class: "table" }, h("tbody", {}, ...Object.entries(caps).map(([k, v]) =>
          h("tr", {}, h("td", {}, k.replace(/_/g, " ")),
            h("td", {}, h("span", { class: `badge ${String(v).startsWith("native") ? "badge-ok" : String(v).startsWith("not") ? "badge-bad" : "badge-warn"}` }, String(v)))))))),
      h("div", { class: "spacer-y" }),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Recent log"), h("div", { class: "spacer" }),
          h("span", { class: "card-sub mono" }, String(s.log_path || ""))),
        h("div", { class: "mono log-box" }, (logs.lines || []).join("\n") || "No log entries yet."))));
}

async function saveSetting(key, value) {
  try {
    await api("/api/settings", { method: "PUT", body: { values: { [key]: value } } });
    toast("Saved");
    refresh();
  } catch (e) { toast(e.message, "error"); }
}
async function setConsent(on) {
  try {
    state.personalisation = await api("/api/personalisation/consent", { method: "POST", body: { enabled: on } });
    toast(on ? "Personalised learning on" : "Personalised learning off");
    refresh();
  } catch (e) { toast(e.message, "error"); }
}
async function resetPersonalisation() {
  if (!confirm("Forget all learned preferences? This cannot be undone.")) return;
  try { await api("/api/personalisation/reset", { method: "POST" }); toast("Preferences reset"); refresh(); }
  catch (e) { toast(e.message, "error"); }
}
async function activateLicence() {
  const el = $("#licence-key");
  const key = el ? el.value : "";
  if (!key.trim()) return toast("Paste a licence key first", "error");
  try {
    state.licence = await api("/api/licence", { method: "POST", body: { key: key.trim(), email: "" } });
    toast(`Licence ${state.licence.status}`);
    refresh();
  } catch (e) { toast(e.message, "error"); }
}

/* -------------------------------------------------------------------- guide */
function viewDocs() {
  const caps = (state.health && state.health.capabilities) || {};
  const limits = (state.health && state.health.limits) || {};
  const step = (n, title, body) => h("div", { class: "numbered" },
    h("span", { class: "num" }, String(n)),
    h("div", {}, h("strong", {}, title), h("p", { class: "card-sub" }, body)));
  return h("div", {},
    h("div", { class: "card" },
      h("h2", {}, "From zero to an edited photograph"),
      h("div", { style: "margin-top:14px" },
        step(1, "Import", "Library → Import. Paste folders or files. RAW files are decoded at full bit depth; originals are never modified."),
        step(2, "Analyse", "Analysis runs on import: scene, lighting, faces, subject, noise, sharpness and white balance."),
        step(3, "Edit", "Open a photo, choose a style, press Edit. Every change appears in the Why list and the adjustment table."),
        step(4, "Deliver", "Send the packet to Lightroom (Plug-in Extras) or export a 16-bit TIFF for a round trip."))),
    h("div", { class: "spacer-y" }),
    h("div", { class: "card" },
      h("h2", {}, "Lightroom Classic installation"),
      h("ol", { class: "ordered" },
        h("li", {}, "Start the engine and leave it running: ", h("code", {}, "kestrel serve")),
        h("li", {}, "In Lightroom Classic: File → Plug-in Manager → Add."),
        h("li", {}, "Select the ", h("code", {}, "lightroom/Kestrel.lrplugin"), " folder in this repository."),
        h("li", {}, "Library → Plug-in Extras → Kestrel → Settings and confirm the engine address (default 127.0.0.1:8765)."),
        h("li", {}, "Select photographs, then Kestrel → Apply latest edit.")),
      h("p", { class: "note", style: "margin-top:10px" },
        "Global develop settings are written natively and remain fully editable in Lightroom. Lightroom's documented SDK " +
        "cannot create local adjustment masks from a plug-in — not a radial gradient and not an AI mask — so Kestrel's masks " +
        "shape the rendered result: pick rendered delivery (16-bit TIFF) when a plan uses them. The capability table in " +
        "Settings is read from the running engine — no invented settings.")),
    h("div", { class: "spacer-y" }),
    h("div", { class: "dash-col" },
      h("div", { class: "card" },
        h("h2", {}, "Implemented today"),
        h("table", { class: "table", style: "margin-top:8px" },
          h("tbody", {}, ...Object.entries(caps).map(([k, v]) => h("tr", {},
            h("td", {}, k.replace(/_/g, " ")),
            h("td", {}, h("span", { class: `badge ${String(v).startsWith("native") ? "badge-ok" : String(v).startsWith("not") ? "badge-bad" : "badge-warn"}` }, String(v)))))))),
      h("div", { class: "card" },
        h("h2", {}, "Honest limits"),
        h("ul", { class: "bullets" },
          h("li", {}, "Subject masks use classical CV (saliency + GrabCut + guided filter). Good, not pixel-perfect. Review confidence before batch-applying a whole season."),
          h("li", {}, "AI denoise and deconvolution are rendered by Kestrel; Adobe exposes no API for Lightroom's own AI Denoise."),
          h("li", {}, "Object removal is not implemented — the plug-in says so rather than shipping a dead button."),
          h("li", {}, "Deep-learning models are optional. Without weights the engine uses calibrated classical methods and states that in the analysis notes."),
          h("li", {}, `Cloud processing: ${limits.cloud_processing ? "available" : "never"}. Everything runs on this machine.`)),
        h("p", { class: "card-sub", style: "margin-top:10px" },
          "Expect a few seconds per photograph at High on a modern laptop, and longer on large RAW files. Quality is prioritised over speed by design."))));
}
