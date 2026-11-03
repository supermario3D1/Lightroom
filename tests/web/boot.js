/* Boot the real workspace in jsdom against a live engine.
 *
 * Run by CI after `kestrel serve` is up (and by hand the same way):
 *     node tests/web/boot.js [baseUrl]
 *
 * It proves the bundled UI actually runs against the bundled API: the shell
 * boots, every navigation destination renders, the editor opens a real
 * photograph, and no console error is emitted. It is a smoke test — it checks
 * wiring and states, not pixels.
 */
const { JSDOM, VirtualConsole } = require("jsdom");

const BASE = process.argv[2] || "http://127.0.0.1:8765";
const VIEWS = ["dashboard", "library", "batch", "profiles", "settings", "docs"];

const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => {
  const message = e.detail ? e.detail.message : e.message;
  if (!/scrollTo|Not implemented/.test(message)) errors.push("jsdom: " + message);
});
vc.on("error", (...args) => errors.push("console.error: " + args.join(" ")));

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function api(path, options) {
  const response = await fetch(BASE + path, options);
  if (!response.ok) throw new Error(`${path} -> HTTP ${response.status}`);
  return response.json();
}

(async () => {
  // A fresh home has no library; load the bundled sample so the library and
  // editor paths have something real to render.
  const demo = await api("/api/demo/load", { method: "POST" });
  const images = (await api("/api/images?limit=5")).images || [];
  if (!images.length) throw new Error("demo library loaded nothing: " + JSON.stringify(demo));
  // Analysis is what the editor's left panel renders, so exercise the real
  // endpoint before the UI boots rather than asserting on an empty state.
  await api(`/api/images/${images[0].id}/analyse?quality=standard`, { method: "POST" });
  // The editor's right-hand panels read the latest *edit*, not the analysis, so
  // render one and keep it: otherwise the tabs legitimately show empty states
  // and the test would assert on them.
  const preview = await api(`/api/images/${images[0].id}/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ quality: "standard", save_version: true }),
  });
  if (!preview.version || !preview.version.metrics) {
    throw new Error("preview did not return a saved version: " + JSON.stringify(Object.keys(preview)));
  }

  const html = await (await fetch(BASE + "/")).text();
  const dom = new JSDOM(html, {
    url: BASE + "/",
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    virtualConsole: vc,
  });
  const window = dom.window;
  const document = window.document;
  window.fetch = (url, opts) => fetch(url.startsWith("http") ? url : BASE + url, opts);
  window.scrollTo = () => {};

  await sleep(2500);

  const failures = [];
  const check = (condition, label) => {
    if (!condition) failures.push(label);
  };
  const text = () => (document.querySelector("#view") || {}).textContent || "";
  const navTo = async (view) => {
    const button = [...document.querySelectorAll(".nav-item")].find((b) => b.dataset.view === view);
    check(Boolean(button), `missing nav item: ${view}`);
    if (!button) return;
    button.click();
    await sleep(1200);
    check(text().trim().length > 20, `view rendered nothing: ${view}`);
    check(!/Something went wrong/.test(text()), `view failed to render: ${view}`);
  };

  // ---- shell -------------------------------------------------------------
  check(document.querySelector("#app") && !document.querySelector("#app").hidden,
    "app is still hidden after boot");
  check(/ok/.test((document.querySelector("#health-chip") || {}).textContent || ""),
    "engine chip does not report ok");
  check(document.querySelectorAll(".nav-item").length === VIEWS.length,
    `expected ${VIEWS.length} navigation destinations`);
  const dashboardCards = document.querySelectorAll("#view .card").length;
  check(dashboardCards > 0, "dashboard rendered no cards");

  // onboarding is a first-run modal; dismiss it if present
  for (let step = 0; step < 3; step += 1) {
    const next = document.querySelector(".modal-actions .btn-primary");
    if (!next) break;
    next.click();
    await sleep(300);
  }

  // ---- every view --------------------------------------------------------
  for (const view of VIEWS) await navTo(view);

  // ---- library -> editor -------------------------------------------------
  await navTo("library");
  const tiles = document.querySelectorAll(".tile");
  check(tiles.length > 0, "library rendered no photographs after the demo import");
  if (tiles.length) {
    tiles[0].click();
    await sleep(2500);
    check(/^#editor\//.test(window.location.hash), "opening a tile did not route to the editor");
    check(Boolean(document.querySelector(".canvas-stage")), "editor rendered no image stage");
    check(document.querySelectorAll(".panel").length >= 3, "editor rendered too few panels");
    check(/What the engine sees/.test(text()), "analysis panel missing");
    check(/Edit this photo|Re-render/.test(text()), "editor offers no edit action");
    check(Boolean(document.querySelector(".panel-tabs")), "editor tabs missing");

    // every analysis tab must render real content, not an empty shell
    const tab = (label) => [...document.querySelectorAll(".panel-tabs button")]
      .find((button) => button.textContent === label);

    const localTab = tab("Local");
    if (localTab) {
      localTab.click();
      await sleep(500);
      check(!/Something went wrong/.test(text()), "mask panel failed to render");
      check(/Click a mask|global adjustments only/.test(text()), "mask panel explains nothing");
      check(document.querySelectorAll(".mask-row").length > 0, "mask panel lists no masks");
      // Clicking a mask must actually fetch and show the overlay — the one
      // feature that lets a photographer judge the segmentation.
      const firstMask = document.querySelector(".mask-row");
      if (firstMask) {
        firstMask.click();
        await sleep(1500);
        check(Boolean(document.querySelector("img.canvas-overlay")),
          "clicking a mask did not draw the overlay");
        check(Boolean(document.querySelector(".overlay-bar")), "overlay has no controls");
        check(!/Something went wrong/.test(text()), "mask overlay failed to render");
      }
    }

    const qualityTab = tab("Quality");
    if (qualityTab) {
      qualityTab.click();
      await sleep(500);
      const metrics = document.querySelectorAll(".metric").length;
      check(metrics >= 8, `quality panel rendered ${metrics} metric rows`);
      check(/Naturalness/.test(text()), "quality panel is missing its metric labels");
    }

    const adjustTab = tab("Adjustments");
    if (adjustTab) {
      adjustTab.click();
      await sleep(500);
      check(/Exposure/.test(text()), "adjustment panel is missing the settings table");
    }

    const whyTab = tab("Why");
    if (whyTab) {
      whyTab.click();
      await sleep(400);
      check(/What the engine sees/.test(text()), "why panel lost its analysis section");
    }
  }

  const report = {
    views: VIEWS.length,
    photographs: images.length,
    cards: dashboardCards,
    errors: errors.slice(0, 10),
    failures,
  };
  console.log(JSON.stringify(report, null, 2));
  window.close();
  process.exit(failures.length === 0 && errors.length === 0 ? 0 : 1);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
