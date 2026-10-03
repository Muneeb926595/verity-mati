#!/usr/bin/env node
// End-to-end check of the Verity UI against a REAL engine, driven through the Chrome DevTools Protocol.
// No dependencies (Node 22+ has fetch + WebSocket built in).
//
//   1. start the engine:   .venv/bin/python -m verity.engine.server --port 8765 --token testtoken
//   2. start a Chrome:     "Google Chrome" --headless=new --remote-debugging-port=9222 --user-data-dir=/tmp/verity-chrome-profile
//   3. run:                node ui/scripts/e2e-cdp.mjs [--shots docs/screenshots] [--only home,answer,...]
//                          (without --shots the screenshots go to <temp>/verity-e2e-shots)
//
// Env: VERITY_URL (default http://127.0.0.1:8765/?token=testtoken), CDP (default http://127.0.0.1:9222),
//      SAMPLE (default <root>/samples/navgps-c),
//      CDP_ATTACH=1 to drive the page already open there instead of a new tab: the desktop app's own window
//      (Windows: start Verity.exe with WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222).
import { cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const URL0 = process.env.VERITY_URL || "http://127.0.0.1:8765/?token=testtoken";
const CDP = process.env.CDP || "http://127.0.0.1:9222";
const SAMPLE = process.env.SAMPLE || join(ROOT, "samples/navgps-c");
const args = process.argv.slice(2);
const argVal = (k, d) => {
  const i = args.indexOf(k);
  return i >= 0 ? args[i + 1] : d;
};
// screenshots go to a temporary folder unless --shots names one (the tracked docs/screenshots only on purpose)
const SHOTS = resolve(argVal("--shots", join(tmpdir(), "verity-e2e-shots")));
const ONLY = (argVal("--only", "") || "").split(",").filter(Boolean);
mkdirSync(SHOTS, { recursive: true });

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok: !!ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}
// a check this machine cannot make (e.g. no sandbox to run code): reported, never counted as passed
function skip(name, why) {
  results.push({ name, ok: true, skipped: true, detail: why });
  console.log(`SKIP  ${name}  — ${why}`);
}
function summary(failed, suffix = "") {
  const skipped = results.filter((r) => r.skipped);
  const why = [...new Set(skipped.map((r) => r.detail))].join("; ");
  return `${results.length - failed.length - skipped.length}/${results.length - skipped.length} checks passed${suffix}` +
    (skipped.length ? ` (${skipped.length} skipped: ${why})` : "");
}

// ------------------------------------------------------------------------------------------ CDP plumbing
const ATTACH = process.env.CDP_ATTACH === "1";
async function openPage() {
  let t;
  if (ATTACH) {
    const pages = (await (await fetch(`${CDP}/json/list`)).json()).filter((x) => x.type === "page");
    t = pages.find((x) => /^http:\/\/127\.0\.0\.1:/.test(x.url)) || pages[0];
    if (!t) throw new Error("no page to attach to at " + CDP);
    console.log("attached to:", t.url.replace(/token=[^&#]+/, "token=…"));
  } else {
    const r = await fetch(`${CDP}/json/new?about:blank`, { method: "PUT" });
    t = await r.json();
  }
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.onopen = res;
    ws.onerror = rej;
  });
  let id = 0;
  const pending = new Map();
  const listeners = [];
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) {
      const { res, rej } = pending.get(d.id);
      pending.delete(d.id);
      d.error ? rej(new Error(d.error.message)) : res(d.result);
    } else if (d.method) listeners.forEach((fn) => fn(d));
  };
  const send = (method, params = {}) =>
    new Promise((res, rej) => {
      const i = ++id;
      pending.set(i, { res, rej });
      ws.send(JSON.stringify({ id: i, method, params }));
    });
  return { send, ws, target: t, on: (fn) => listeners.push(fn) };
}

let P;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function evaluate(expr) {
  const r = await P.send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error("eval failed: " + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
}

async function waitFor(expr, timeout = 15000, label = expr) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try {
      const v = await evaluate(expr);
      if (v) return v;
    } catch {
      /* page may be navigating */
    }
    await sleep(100);
  }
  throw new Error("timeout waiting for: " + label);
}

async function setViewport(width, height, dark = false) {
  await P.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 2, mobile: width < 600 });
  await P.send("Emulation.setEmulatedMedia", {
    features: [
      { name: "prefers-color-scheme", value: dark ? "dark" : "light" },
      { name: "prefers-reduced-motion", value: "reduce" },
    ],
  });
}

/** Real mouse click at the centre of the first element matching `js` (an expression returning an Element). */
async function click(js, label = js) {
  const box = await waitFor(
    `(() => { const el = (${js}); if (!el) return null; el.scrollIntoView({block: "center"}); const r = el.getBoundingClientRect(); return r.width ? {x: r.x + r.width/2, y: r.y + r.height/2} : null; })()`,
    10000,
    label,
  );
  await sleep(60);
  for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
    await P.send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
  }
}

async function typeText(text) {
  await P.send("Input.insertText", { text });
}

async function pressEnter() {
  const base = { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 };
  await P.send("Input.dispatchKeyEvent", { type: "keyDown", ...base, text: "\r", unmodifiedText: "\r" });
  await P.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
}

async function pressEscape() {
  const base = { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 };
  await P.send("Input.dispatchKeyEvent", { type: "keyDown", ...base });
  await P.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
}

async function shot(name, { full = false } = {}) {
  if (ONLY.length && !ONLY.some((o) => name.includes(o))) return;
  await sleep(150);
  let params = { format: "png" };
  if (full) {
    const m = await P.send("Page.getLayoutMetrics");
    const h = Math.min(Math.ceil(m.cssContentSize.height), 12000);
    const w = Math.ceil(m.cssLayoutViewport.clientWidth);
    params = { format: "png", captureBeyondViewport: true, clip: { x: 0, y: 0, width: w, height: h, scale: 1 } };
  }
  const r = await P.send("Page.captureScreenshot", params);
  const file = join(SHOTS, name + ".png");
  writeFileSync(file, Buffer.from(r.data, "base64"));
  console.log("      screenshot → " + file);
}

async function rpc(method, params = {}) {
  return evaluate(`(async () => {
    const tok = new URLSearchParams(location.search).get("token");
    const r = await fetch("/rpc", {method: "POST", headers: {Authorization: "Bearer " + tok, "Content-Type": "application/json"},
      body: JSON.stringify({jsonrpc: "2.0", id: 1, method: ${JSON.stringify(method)}, params: ${JSON.stringify(params)}})});
    return (await r.json());
  })()`);
}

const text = (sel) => `(document.querySelector(${JSON.stringify(sel)})?.innerText || "")`;
const byText = (sel, t) =>
  `[...document.querySelectorAll(${JSON.stringify(sel)})].find(e => e.innerText.trim().includes(${JSON.stringify(t)}))`;

async function goto(url) {
  await P.send("Page.navigate", { url });
  await waitFor(`document.readyState === "complete"`);
}

// ------------------------------------------------------------------------------------------ the journey
async function main() {
  P = await openPage();
  await P.send("Page.enable");
  await P.send("Runtime.enable");
  const consoleErrors = [];
  P.on((m) => {
    if (m.method === "Runtime.exceptionThrown") consoleErrors.push(m.params.exceptionDetails?.exception?.description || "exception");
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
      consoleErrors.push(m.params.args.map((a) => a.value ?? a.description).join(" "));
  });
  await setViewport(1280, 860);

  const info = (await (async () => {
    await goto(URL0);
    return rpc("engine.info");
  })()).result || {};
  const settingsBefore = info.settings || {};
  const sandboxOk = !!info.probe?.tools?.sandbox?.ok;          // scenario questions run the code only with a sandbox
  console.log("engine settings before:", JSON.stringify(settingsBefore), "· sandbox:", sandboxOk);
  // the home screen lists recent projects: the sample must be one of them and indexed (a fresh data folder has none)
  if (!(await rpc("project.open", { path: SAMPLE })).result?.ready) {
    await rpc("index.start", { path: SAMPLE });
    for (let i = 0; i < 360 && !(await rpc("project.status", { path: SAMPLE })).result?.ready; i++) await sleep(500);
  }

  // ---------------------------------------------------------------- home
  await goto(URL0.split("#")[0]);
  await waitFor(`document.querySelector(".hero-title")?.innerText === "Verity"`, 20000, "home screen");
  await waitFor(`document.querySelectorAll(".recent-item").length > 0 || document.querySelector(".empty")`, 10000, "recent list");
  // inside the desktop app, projects open with the native folder picker and the typed-path field is one click away
  const inApp = await evaluate(`!!window.__TAURI_INTERNALS__?.invoke`);
  const showField = async () => {
    if (inApp && !(await evaluate(`!!document.querySelector("#folder-path")`)))
      await click(byText(".link-btn", "or type a folder path"), "the 'or type a folder path' link");
    await waitFor(`!!document.querySelector("#folder-path")`, 5000, "folder field");
  };
  check(inApp ? "home shows title, subtitle, Open Project (native folder picker), recent list"
              : "home shows title, subtitle, folder field, recent list",
    (await evaluate(text(".hero-sub"))) === "Understand your C/C# codebase" &&
    (inApp ? await evaluate(`!![...document.querySelectorAll("button")].find((b) => b.innerText.trim() === "Open Project")`)
           : await evaluate(`!!document.querySelector("#folder-path")`)) &&
    (await evaluate(`document.querySelectorAll(".recent-item").length`)) >= 1);
  const recentRow = await evaluate(`[...document.querySelectorAll(".recent-item")].map(e => e.innerText.replace(/\\s+/g, " ")).join(" | ")`);
  check("recent row shows name, status label and language", /navgps-c/.test(recentRow) && /Ready/.test(recentRow), recentRow.slice(0, 160));
  await shot("01-home");
  await setViewport(1280, 860, true);
  await shot("01-home-dark");
  await setViewport(1280, 860);

  // error path: a folder that does not exist → friendly inline banner
  await showField();
  await click(`document.querySelector("#folder-path")`);
  await typeText("/no/such/folder");
  await pressEnter();
  await waitFor(`!!document.querySelector(".hero .banner.error")`, 8000, "error banner");
  check("opening a missing folder shows the engine's message inline", /Folder not found/.test(await evaluate(text(".hero .banner.error"))),
    await evaluate(text(".hero .banner.error")));
  await shot("02-home-error");

  // ---------------------------------------------------------------- open the sample via the path field (Enter submits)
  await showField();
  await evaluate(`(() => { const i = document.querySelector("#folder-path"); i.select(); })()`);
  await typeText(SAMPLE);
  await pressEnter();
  await waitFor(`!!document.querySelector(".proj-head h1")`, 10000, "project view");
  await waitFor(`/Ready|Indexing/.test(${text(".proj-actions .pill")})`, 20000, "status pill");
  // if it had to (re)index, wait for Ready
  await waitFor(`${text(".proj-actions .pill")}.trim() === "Ready"`, 120000, "Ready");
  check("sample opens and shows ● Ready", (await evaluate(text(".proj-actions .pill"))).trim() === "Ready");
  check("URL keeps the project in the hash", (await evaluate("location.hash")).includes("navgps-c"));
  check("example question chips are shown", (await evaluate(`document.querySelectorAll(".chip").length`)) >= 5);
  await shot("03-project-ready");

  // ---------------------------------------------------------------- indexing progress (full re-index started from outside the UI)
  const prevEmb = settingsBefore.embeddings;
  await rpc("settings.set", { embeddings: true }); // makes the last phase long enough to watch
  const started = await rpc("index.start", { path: SAMPLE, full: true });
  check("index.start (full) accepted", !!started.result?.id, started.error?.message || "");
  await waitFor(`!!document.querySelector(".index-card")`, 10000, "progress card appears for an externally started index");
  check("progress card appears for an index started outside the UI", true);
  await waitFor(`/parts complete/.test(${text(".index-headline")})`, 15000, "headline");
  const headline = await evaluate(text(".index-headline"));
  check("friendly headline 'Understanding your C project… N of 7 parts complete'", /^Understanding your C project… \d of 7 parts complete$/.test(headline), headline);
  await click(byText(".index-card .link-btn", "Show details"));
  await waitFor(`document.querySelectorAll(".phase").length === 7`, 5000, "7 phases");
  // try to catch a moment with a counter / ETA visible
  try {
    await waitFor(`!!document.querySelector(".index-eta") && !!document.querySelector(".index-count")`, 30000, "counter + ETA");
  } catch {
    try {
      await waitFor(`!!document.querySelector(".index-count")`, 5000, "counter");
    } catch {
      /* small projects can be too quick */
    }
  }
  await shot("04-indexing-progress");
  const phasesText = await evaluate(`[...document.querySelectorAll(".phase")].map(p => p.className.replace("phase ", "") + ":" + p.querySelector(".phase-label").innerText).join(" | ")`);
  check("details list the 7 phases with states", phasesText.split("|").length === 7, phasesText);
  await waitFor(`!document.querySelector(".index-card")`, 180000, "indexing finished");
  await waitFor(`${text(".proj-actions .pill")}.trim() === "Ready"`, 10000, "Ready after indexing");
  await sleep(200);
  check("after indexing: Ready and the question box has focus", await evaluate(`document.activeElement === document.querySelector(".ask-input")`));

  // cancel works: start another full index from the UI's own path (Update isn't shown when fresh, so use the RPC) and press Cancel
  await rpc("index.start", { path: SAMPLE, full: true });
  await waitFor(`!!document.querySelector(".index-card")`, 10000, "progress card (2)");
  await click(byText(".index-card .btn", "Cancel"));
  await waitFor(`!document.querySelector(".index-card")`, 60000, "cancelled");
  await sleep(300);
  const afterCancel = await evaluate(text(".proj-actions"));
  const noticeCancel = await evaluate(text(".content > .banner"));
  check("Cancel stops indexing and offers Update", /Update/.test(afterCancel) || /Stopped/.test(noticeCancel) || /Ready/.test(afterCancel),
    (afterCancel + " / " + noticeCancel).replace(/\s+/g, " ").slice(0, 200));
  await shot("05-indexing-cancelled");
  if (await evaluate(`!!${byText(".proj-actions .btn", "Update")}`)) {
    await click(byText(".proj-actions .btn", "Update"));
    await waitFor(`!!document.querySelector(".index-card")`, 10000, "update started");
    await waitFor(`!document.querySelector(".index-card")`, 180000, "update finished");
    await waitFor(`${text(".proj-actions .pill")}.trim() === "Ready"`, 10000, "Ready after update");
    check("Update finishes the index and returns to Ready", true);
  }
  await rpc("settings.set", { embeddings: prevEmb ?? false });

  // ---------------------------------------------------------------- ask: flow question (Enter submits)
  await click(`document.querySelector(".ask-input")`);
  await evaluate(`document.querySelector(".ask-input").select()`);
  await typeText("Explain how GPS data travels through this application.");
  await pressEnter();
  await waitFor(`!!document.querySelector(".answer .flow")`, 120000, "flow answer");
  const sections = await evaluate(`[...document.querySelectorAll(".answer .eyebrow")].map(e => e.childNodes[0].textContent.trim())`);
  check("answer sections in order", JSON.stringify(sections) === JSON.stringify(["Your question", "Answer", "How the data flows", "Evidence", "Is this the right place?"]), JSON.stringify(sections));
  const flowSteps = await evaluate(`document.querySelectorAll(".flow-step").length`);
  check("flow chain rendered with steps + file:line", flowSteps >= 5 && (await evaluate(`document.querySelectorAll(".flow-step .loc").length`)) >= 5, `${flowSteps} steps`);
  check("inline code rendered from backticks", (await evaluate(`document.querySelectorAll(".prose code.ic").length`)) > 0);
  check("certainty summary line", /confirmed|inferred/.test(await evaluate(text(".certainty-line"))), await evaluate(text(".certainty-line")));
  check("five collapsed sections", (await evaluate(`[...document.querySelectorAll(".coll-head")].filter(b => b.getAttribute("aria-expanded") === "false").length`)) === 5);
  await evaluate(`window.scrollTo(0, 0)`);
  await shot("06-answer");
  await shot("06-answer-full", { full: true });

  // expand all five
  for (const t of ["Show reasoning / evidence", "Show execution path", "Show related code", "Show assumptions", "Show verification"]) {
    await click(byText(".coll-head", t), t);
  }
  await sleep(200);
  check("all five sections expand", (await evaluate(`document.querySelectorAll(".coll-body").length`)) === 5);
  const verify = await evaluate(text(".verify"));
  check("verification in plain words", /of \d+ statements? re-checked against the code/.test(verify), verify.split("\n")[0]);
  await evaluate(`document.querySelector(".details").scrollIntoView({block: "start"}); window.scrollBy(0, -60)`);
  await shot("07-sections-expanded");
  await shot("07-sections-expanded-full", { full: true });

  // evidence → source viewer at nav/navigation.c
  await click(`[...document.querySelectorAll(".ev")].find(b => b.querySelector(".ev-loc")?.innerText.startsWith("nav/navigation.c"))`, "evidence nav/navigation.c");
  await waitFor(`!!document.querySelector(".source-panel .row.target")`, 10000, "source viewer");
  const src = await evaluate(`({file: document.querySelector(".src-file").innerText, line: document.querySelector(".row.target .ln").innerText,
      visible: (() => { const r = document.querySelector(".row.target").getBoundingClientRect(); const b = document.querySelector(".src-body").getBoundingClientRect(); return r.top >= b.top && r.bottom <= b.bottom; })(),
      code: document.querySelector(".row.target .tx").innerText})`);
  check("source viewer opens nav/navigation.c with the line highlighted and in view", src.file === "nav/navigation.c" && src.visible, `${src.file}:${src.line}  ${src.code.trim()}`);
  await shot("08-source-viewer");
  await pressEscape();
  await waitFor(`!document.querySelector(".source-panel")`, 3000, "viewer closes on Escape");
  check("Escape closes the source viewer", true);

  // ---------------------------------------------------------------- ask: scenario question (sandboxed run)
  await evaluate(`window.scrollTo(0, 0)`);
  await click(`document.querySelector(".ask-input")`);
  await evaluate(`document.querySelector(".ask-input").select()`);
  await typeText("What happens when GPS sends heading 270, speed 80, valid true?");
  await click(`document.querySelector(".ask-btn")`);
  // the ask progress line should appear (the run takes ~2 s)
  let sawProgress = false;
  try {
    await waitFor(`!!document.querySelector(".ask-progress")`, 3000, "ask progress");
    sawProgress = true;
    await shot("09-asking");
  } catch {
    /* too quick to see */
  }
  if (sawProgress) check("ask progress line shown while answering", true);
  else if (await evaluate(`/heading 270/.test(${text(".question-text")}) && !document.querySelector(".ask-progress") && !!document.querySelector(".answer .prose")`))
    skip("ask progress line shown while answering", "the answer was back before the progress line could be seen"); // no sandbox run to wait for
  else check("ask progress line shown while answering", false);
  const RUN_CHECKS = ["sandboxed run shown: inputs, outputs (motor_set_steering(50)), final values (currentHeading 337.5)",
    "OBSERVED steps are marked 'ran'"];
  if (sandboxOk) {
    await waitFor(`/heading = 270/.test(${text(".answer .prose")})`, 180000, "scenario answer");
    await click(byText(".coll-head", "Show execution path"));
    await waitFor(`!!document.querySelector(".run-table")`, 5000, "run table");
    const run = await evaluate(text(".run-table"));
    check(RUN_CHECKS[0],
      /heading/.test(run) && /motor_set_steering\(50\)/.test(run) && /337\.5/.test(run), run.replace(/\s+/g, " ").slice(0, 200));
    check(RUN_CHECKS[1], (await evaluate(`document.querySelectorAll(".badge.ran").length`)) > 0);
    await evaluate(`document.querySelector(".runs").scrollIntoView({block: "center"})`);
    await shot("10-scenario-run");
    await evaluate(`window.scrollTo(0, 0)`);
    await shot("10-scenario-answer-full", { full: true });
  } else {
    // no sandbox (Windows for now): the engine answers by reading the code, and must not pretend it ran anything
    await waitFor(`/heading 270/.test(${text(".question-text")}) && !document.querySelector(".ask-progress") && ` +
      `/ProcessGpsData|CalculateHeading/.test(${text(".answer .prose")})`, 180000, "scenario answer (read from the code)");
    const prose = await evaluate(text(".answer .prose"));
    check("without a sandbox the scenario is answered from the code, with no run shown",
      !(await evaluate(`!!document.querySelector(".run-table") || !!document.querySelector(".badge.ran")`)),
      prose.replace(/\s+/g, " ").slice(0, 160));
    for (const name of RUN_CHECKS) skip(name, "no sandbox to run code on this machine");
  }

  // ---------------------------------------------------------------- history + settings
  await evaluate(`window.scrollTo(0, 0)`);
  await click(byText(".menu .btn", "Recent questions"));
  await waitFor(`document.querySelectorAll(".menu-item").length > 0`, 5000, "history list");
  check("recent questions list", (await evaluate(`document.querySelectorAll(".menu-item").length`)) >= 2);
  await shot("11-history");
  await click(byText(".menu-item", "Explain how GPS data travels"));
  await waitFor(`/Explain how GPS data travels/.test(${text(".question-text")})`, 10000, "stored answer loads");
  check("a past answer re-opens from history (answer.get)", true);

  await click(`document.querySelector(".gear")`);
  await waitFor(`document.querySelectorAll(".machine li").length > 0`, 15000, "settings");
  const st = await evaluate(text(".modal"));
  check("settings show wording modes, machine lines and model label", /Facts only/.test(st) && /Auto/.test(st) && /Fast/.test(st) && /AI model/.test(st),
    st.replace(/\s+/g, " ").slice(0, 160));
  await shot("12-settings");
  await pressEscape();
  await waitFor(`!document.querySelector(".modal")`, 3000, "settings close");

  // ---------------------------------------------------------------- narrow screens + dark
  await setViewport(390, 844);
  await evaluate(`window.scrollTo(0, 0)`);
  await sleep(200);
  check("no horizontal scroll at phone width", await evaluate(`document.documentElement.scrollWidth <= window.innerWidth + 1`));
  await shot("13-phone-answer");
  await click(`document.querySelector(".flow-step .loc") || document.querySelector(".ev")`);
  await waitFor(`!!document.querySelector(".source-panel .row.target")`, 10000, "phone source modal");
  await shot("14-phone-source");
  await pressEscape();
  await setViewport(1280, 860, true);
  await evaluate(`window.scrollTo(0, 0)`);
  await shot("15-answer-dark");
  await setViewport(1280, 860);

  // ---------------------------------------------------------------- learning from corrections (on a throwaway copy)
  const LEARN = join(mkdtempSync(join(tmpdir(), "verity-e2e-learn-")), "navgps-c");
  cpSync(SAMPLE, LEARN, { recursive: true });
  try {
    await rpc("project.open", { path: LEARN });
    await rpc("index.start", { path: LEARN, full: true });
    for (let i = 0; i < 360 && !(await rpc("project.status", { path: LEARN })).result?.ready; i++) await sleep(500);
    await goto(URL0.split("#")[0] + "#/project/" + encodeURIComponent(LEARN));
    await waitFor(`${text(".proj-actions .pill")}.trim() === "Ready"`, 60000, "learning copy ready");
    const LQ = "What happens when the checksum of an NMEA sentence is wrong?";
    const askQ = async (q) => {
      await click(`document.querySelector(".ask-input")`);
      await evaluate(`document.querySelector(".ask-input").select()`);
      await typeText(q);
      await pressEnter();
    };
    await askQ(LQ);
    await waitFor(`/gps_receiver_process_line/.test(${text(".answer .prose")}) && !document.querySelector(".ask-progress")`, 60000, "failure answer");
    check("an answer offers 'Is this the right place?' with the right was right / pick a line actions",
      await evaluate(`!!document.querySelector(".learn-card") && /The answer was right/.test(${text(".learn-card")}) && /pick a line/.test(${text(".learn-card")})`));
    await click(byText(".learn-card button", "pick a line"));
    await waitFor(`!!document.querySelector(".source-panel.picking .row")`, 10000, "pick mode");
    check("pick mode opens the source viewer asking which line answers the question",
      /Which line answers your question/.test(await evaluate(text(".src-pickhead"))));
    // the right line is in another file: go to it by function name
    await click(`document.querySelector(".pick-find input")`);
    await typeText("nmea_checksum_ok");
    await waitFor(`[...document.querySelectorAll(".pick-results button")].some(b => /nmea_checksum_ok/.test(b.innerText))`, 10000, "function search results");
    await click(byText(".pick-results button", "nmea_checksum_ok"));
    await waitFor(`/nmea_parser\.c/.test(${text(".src-file")}) && !!document.querySelector(".source-panel.picking .row")`, 10000, "other file");
    check("pick mode can go to another file by function name", true);
    await click(`[...document.querySelectorAll(".source-panel.picking .row")].find(r => r.querySelector(".ln")?.innerText.trim() === "26")`, "line 26");
    await click(`document.querySelector(".pick-note")`);
    await typeText("the *hh field itself is checked here");
    await shot("19-pick-a-line");
    await click(byText(".src-pickbar button", "Use this line"));
    await waitFor(`!!document.querySelector(".learned-note") && !document.querySelector(".ask-progress")`, 60000, "corrected answer");
    const note = await evaluate(text(".learned-note"));
    check("the answer asked again uses the correction and says so (id, note, Undo)",
      /c-0001/.test(note) && /hh field/.test(note) && /Undo/.test(note) && /nmea_checksum_ok/.test(await evaluate(text(".answer .prose"))), note.slice(0, 160));
    check("a banner confirms the saved correction with an Undo button", /Saved correction c-0001/.test(await evaluate(`document.body.innerText`)));
    await shot("20-corrected-answer");
    check("alternatives are offered, each with a 'This one' button",
      await evaluate(`document.querySelectorAll(".alts .alt").length > 0 && [...document.querySelectorAll(".alts .alt")].every(li => [...li.querySelectorAll("button")].some(b => /This one/.test(b.innerText)))`),
      (await evaluate(text(".learn-card"))).replace(/\s+/g, " ").slice(0, 200));
    await click(`document.querySelector(".learn-btn")`);
    await waitFor(`!!document.querySelector(".learn-panel .corr")`, 10000, "learned panel");
    const panel = await evaluate(text(".learn-panel"));
    check("the Learned panel lists the correction (status, question, right place)",
      /c-0001/.test(panel) && /in use/.test(panel) && /nmea_parser\.c:26/.test(panel), panel.replace(/\s+/g, " ").slice(0, 160));
    await shot("21-learned-panel");
    await click(byText(".learn-panel .tab", "Glossary"));
    await waitFor(`/Add a term/.test(${text(".learn-panel")})`, 5000, "glossary tab");
    await click(byText(".learn-panel .tab", "Share"));
    await waitFor(`/Export test suite/.test(${text(".learn-panel")})`, 5000, "share tab");
    check("the panel's Glossary and Share tabs open", true);
    await pressEscape();
    await waitFor(`!document.querySelector(".learn-panel")`, 3000, "panel closes");
    await click(byText(".learned-note button", "Undo"));
    await waitFor(`!document.querySelector(".learned-note") && /gps_receiver_process_line/.test(${text(".answer .prose")}) && !document.querySelector(".ask-progress")`, 60000, "answer after undo");
    check("Undo stops using the correction and the answer is the analysis' own again", true);
    await click(byText(".learn-card button", "The answer was right"));
    await waitFor(`/test case/.test(${text(".learn-card")})`, 10000, "confirmation saved");
    check("'The answer was right' keeps the answer as a test case", true);
    const lst = (await rpc("corrections.list", { path: LEARN })).result;
    check("engine state matches: 1 undone correction, 1 confirmation", lst.counts.removed === 1 && lst.counts.confirmations === 1, JSON.stringify(lst.counts));
    await setViewport(390, 844);
    check("the learning card fits a phone screen", await evaluate(`document.documentElement.scrollWidth <= window.innerWidth + 1`));
    await setViewport(1280, 860);
  } finally {
    await rpc("project.forget", { path: LEARN });
    try {
      rmSync(dirname(LEARN), { recursive: true, force: true });
    } catch {
      /* best effort */
    }
  }

  // ---------------------------------------------------------------- console hygiene
  check("no uncaught errors in the page console", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${summary(failed)}`);
  try {
    if (!ATTACH) await fetch(`${CDP}/json/close/${P.target.id}`);   // never close the app's own window
  } catch {
    /* ignore */
  }
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => {
  console.error("E2E aborted:", e.message);
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${summary(failed, " before the abort")}`);
  process.exit(2);
});
