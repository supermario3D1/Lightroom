/* Kestrel — application shell.
 *
 * No framework and no build step on purpose: the app ships inside the package,
 * must work offline on a photographer's workstation, and must stay auditable.
 * Every control is wired to the local REST API — there are no mock buttons and
 * no simulated progress.
 */
"use strict";

/* ------------------------------------------------------------------ helpers */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v !== null && v !== undefined) el.setAttribute(k, v);
  }
  for (const kid of kids.flat(9)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const fmt = {
  pct: (v, d = 0) => `${(Number(v || 0) * 100).toFixed(d)}%`,
  num: (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(d)),
  date: (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? String(iso) : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  },
  ago: (iso) => {
    if (!iso) return "";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return `${Math.round(s)}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  },
  bytes: (b) => {
    if (!b) return "—";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0, v = Number(b);
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
  },
  dur: (s) => {
    if (s === null || s === undefined) return "—";
    if (s < 90) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
    return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  },
};

async function api(path, opts = {}) {
  const init = { headers: {}, ...opts };
  if (init.body && typeof init.body !== "string" && !(init.body instanceof FormData)) {
    init.body = JSON.stringify(init.body);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { detail: text }; }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

let toastTimer = null;
function toast(message, kind = "") {
  const el = $("#toast");
  if (!el) return;
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, kind === "error" ? 6500 : 3800);
}

/* ------------------------------------------------------------------- state */
const state = {
  view: "dashboard",
  health: null,
  stats: null,
  collections: [],
  images: [],
  filters: { collection: "", status: "", search: "" },
  selection: new Set(),
  editor: null,
  jobs: null,
  profiles: [],
  settings: null,
  licence: null,
  personalisation: null,
  batch: null,
  busy: false,
  renderToken: 0,
};

const VIEW_META = {
  dashboard: ["Dashboard", "Library health, queue and recent work"],
  library: ["Library", "Every photograph, analysed individually"],
  editor: ["Library", "Open a photograph to edit it"],
  batch: ["Batch", "Consistent grading for a whole collection"],
  profiles: ["Profiles", "Genre-adaptive intelligence, saved and versioned"],
  settings: ["Settings", "Quality, performance, privacy and licence"],
  docs: ["Guide", "Setup, Lightroom workflow and honest limits"],
};

/** The editor is a detail view of the library, so it keeps that nav context. */
function navView(view = state.view) {
  return view === "editor" ? "library" : view;
}

function intentLabel(i) {
  return { automatic: "Auto", natural: "Natural", vibrant: "Vibrant",
           dramatic: "Dramatic", editorial: "Editorial" }[i] || i;
}

/* ------------------------------------------------------------------- shell */
function renderShell() {
  const activeNav = navView();
  $$("#nav .nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === activeNav);
  });
  const [title, sub] = VIEW_META[state.view] || VIEW_META.dashboard;
  $("#top-title").textContent = state.view === "editor" && state.editor && state.editor.image
    ? (state.editor.image.filename || title) : title;
  $("#top-sub").textContent = sub;
  const hw = state.health && state.health.hardware;
  $("#hw-chip").textContent = hw ? `${hw.cpu_count} cores · ${hw.total_memory_gb} GB` : "—";
  const chip = $("#health-chip");
  if (state.health) {
    chip.textContent = `engine ${state.health.version} · ok`;
    chip.className = "chip chip-ok";
  } else {
    chip.textContent = "engine offline";
    chip.className = "chip chip-bad";
  }
  const lic = state.licence || {};
  const licChip = $("#licence-chip");
  licChip.textContent = lic.status || "trial";
  licChip.className = `chip ${lic.status === "active" ? "chip-ok" : lic.status === "trial" ? "chip-warn" : "chip-bad"}`;
  const q = state.jobs;
  const qChip = $("#queue-chip");
  if (!q || !q.jobs || !q.jobs.length) {
    qChip.textContent = "idle";
    qChip.className = "chip chip-muted";
  } else {
    const active = q.jobs.find((j) => ["running", "queued", "paused"].includes(j.status));
    qChip.textContent = active ? `${active.status} ${active.completed}/${active.total}` : "idle";
    qChip.className = `chip ${active && active.status === "running" ? "chip-warn" : "chip-muted"}`;
  }
}

function setBusy(on) {
  state.busy = on;
  document.body.style.cursor = on ? "progress" : "";
}

/* ------------------------------------------------------------------- modal */
function openModal(title, body, actions = []) {
  const modal = $("#modal");
  modal.hidden = false;
  modal.innerHTML = "";
  const card = h("div", { class: "modal-card" },
    h("div", { class: "modal-head" },
      h("h2", {}, title),
      h("div", { class: "spacer" }),
      h("button", { class: "btn btn-ghost btn-sm", onclick: closeModal, "aria-label": "Close" }, "✕")),
    body,
    actions.length ? h("div", { class: "modal-actions" }, ...actions) : null);
  modal.append(card);
  modal.onclick = (e) => { if (e.target === modal) closeModal(); };
  return card;
}
function closeModal() {
  const modal = $("#modal");
  modal.hidden = true;
  modal.innerHTML = "";
}

/* ------------------------------------------------------------------ router */
async function refresh() {
  try { state.health = await api("/api/health"); } catch (_) { state.health = null; }
  try { state.licence = await api("/api/licence"); } catch (_) { /* ignore */ }
  try { state.jobs = await api("/api/jobs"); } catch (_) { /* ignore */ }
  render();
}

async function render() {
  renderShell();
  const view = $("#view");
  const token = ++state.renderToken;
  const fn = {
    dashboard: viewDashboard, library: viewLibrary, editor: viewEditor,
    batch: viewBatch, profiles: viewProfiles, settings: viewSettings, docs: viewDocs,
  }[state.view] || viewDashboard;
  try {
    const node = await fn();
    if (token !== state.renderToken) return;   // a newer render already won
    view.innerHTML = "";
    view.append(node);
  } catch (e) {
    if (token !== state.renderToken) return;
    view.innerHTML = "";
    view.append(h("div", { class: "empty card" }, h("h2", {}, "Something went wrong"),
      h("p", {}, e.message), h("button", { class: "btn", onclick: () => refresh() }, "Retry")));
  }
}

function navigate(view) {
  if (view === "editor") {
    const id = state.editor && state.editor.id;
    if (!id) {
      // No photo chosen yet: land on the library rather than an empty editor.
      if (location.hash !== "#library") location.hash = "#library";
      else { state.view = "library"; render(); }
      return;
    }
    if (location.hash !== `#editor/${id}`) location.hash = `#editor/${id}`;
    else { state.view = "editor"; render(); }
    return;
  }
  state.view = view;
  if (location.hash !== `#${view}`) location.hash = `#${view}`;
  else render();
}

function routeFromHash() {
  const raw = (location.hash || "#dashboard").slice(1);
  const [view, arg] = raw.split("/");
  if (view === "editor") {
    if (arg && (!state.editor || state.editor.id !== arg)) openEditor(arg);
    else { state.view = "editor"; render(); }
    return;
  }
  state.view = ["dashboard", "library", "batch", "profiles", "settings", "docs"].includes(view)
    ? view : "dashboard";
  render();
}

/* --------------------------------------------------------------- onboarding */
async function maybeOnboarding() {
  let s = state.settings;
  if (!s) { try { s = (await api("/api/settings")).settings; state.settings = s; } catch (_) { return; } }
  if (s.onboarding_complete) return;
  const stepBox = h("div", {});
  const next = h("button", { class: "btn btn-primary" }, "Continue");
  let step = 0;
  const draw = () => {
    stepBox.innerHTML = "";
    if (step === 0) {
      stepBox.append(
        h("p", { class: "card-sub", style: "line-height:1.7" },
          "Kestrel analyses every photograph — scene, light, faces, noise, colour — before touching a slider. " +
          "It explains each change, keeps the original untouched, and never uploads your work."),
        h("p", { class: "note", style: "margin-top:10px" }, "All processing happens on this machine."));
      next.textContent = "Continue";
    } else if (step === 1) {
      stepBox.append(h("div", { class: "field" },
        h("label", {}, "Default quality"),
        h("select", { id: "ob-quality" },
          ...Object.entries((state.health || {}).quality_presets || {}).map(([k, p]) =>
            h("option", { value: k, selected: k === (state.settings.default_quality || "high") },
              `${p.label} — ~${p.est_seconds}s per photo`)))),
        h("p", { class: "card-sub" }, "You can change this per image and per batch at any time."));
      next.textContent = "Continue";
    } else {
      stepBox.append(h("p", { class: "card-sub", style: "line-height:1.7" },
        "Start with the sample gallery to see the engine work, or import your own photographs. " +
        "For Lightroom, open the Guide for the three-step plug-in installation."));
      next.textContent = "Start";
    }
  };
  next.onclick = async () => {
    if (step < 2) { step += 1; draw(); return; }
    const q = $("#ob-quality");
    try {
      await api("/api/settings", { method: "PUT", body: { values: { onboarding_complete: true, ...(q ? { default_quality: q.value } : {}) } } });
    } catch (_) { /* non-fatal */ }
    closeModal();
    refresh();
  };
  draw();
  openModal("Welcome to Kestrel", stepBox, [next]);
}

/* ------------------------------------------------------------------- boot */
async function boot() {
  try {
    state.health = await api("/api/health");
    state.licence = await api("/api/licence").catch(() => null);
    state.jobs = await api("/api/jobs").catch(() => null);
    const s = await api("/api/settings");
    state.settings = s.settings;
    $("#brand-version").textContent = `v${state.health.version}`;
    $("#app").hidden = false;
    $("#boot").hidden = true;
    await render();
    setInterval(() => {
      api("/api/jobs").then((j) => {
        state.jobs = j;
        renderShell();
        if (state.view === "batch" || state.view === "dashboard") {
          const active = (j.jobs || []).find((x) => ["running", "queued"].includes(x.status));
          if (active) render();
        }
      }).catch(() => {});
    }, 4000);
    maybeOnboarding();
  } catch (e) {
    $("#boot-msg").textContent = `Cannot reach the Kestrel engine (${e.message}). Start it with “kestrel serve”.`;
    setTimeout(boot, 3000);
  }
}

window.addEventListener("hashchange", routeFromHash);
document.addEventListener("click", (e) => {
  const nav = e.target.closest("[data-view]");
  if (!nav) return;
  e.preventDefault();
  navigate(nav.dataset.view);
});
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea, select")) return;
  const ed = state.editor;
  if (!ed || state.view !== "editor") return;
  if (e.key === "b") { ed.compare = ed.compare > 0 ? 0 : 50; render(); }
  if (e.key === "ArrowLeft") { ed.compare = Math.max(0, ed.compare - 5); render(); }
  if (e.key === "ArrowRight") { ed.compare = Math.min(100, ed.compare + 5); render(); }
  if (e.key === "Enter") runEdit(false);
  if (e.key === "a") sendFeedback(ed, "accept");
  if (e.key === "r") sendFeedback(ed, "reject");
});

boot();
