// Screenshots 16-18 for docs/screenshots: the three UI states ui/scripts/e2e-cdp.mjs does not cover, captured from a
// real engine through the Chrome DevTools Protocol (same viewport and scale as e2e-cdp.mjs). No dependencies.
//   STEP=stale      17-stale-files (a project with one file changed after indexing) and
//                   18-source-stale (the source viewer's warning on that file)
//   STEP=reconnect  16-reconnecting (the engine stops while the page is open; this script stops ENGINE_PID)
// Run it like e2e-cdp.mjs (engine on 8765 with token "testtoken", headless Chrome on 9222), for example:
//   cp -R samples/navgps-c /tmp/stale-copy
//   SHOTS=docs/screenshots STEP=stale STALE=/tmp/stale-copy/navgps-c node ui/scripts/extra-shots.mjs
//   SHOTS=docs/screenshots STEP=reconnect SAMPLE=$PWD/samples/navgps-c ENGINE_PID=<engine pid> node ui/scripts/extra-shots.mjs
// Env: URL0 (default http://127.0.0.1:8765/?token=testtoken), CDP (default http://127.0.0.1:9222).
import { appendFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const URL0 = process.env.URL0 || "http://127.0.0.1:8765/?token=testtoken";
const CDP = process.env.CDP || "http://127.0.0.1:9222";
const SHOTS = process.env.SHOTS;
const STEP = process.env.STEP;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const t = await (await fetch(`${CDP}/json/new?about:blank`, { method: "PUT" })).json();
const ws = new WebSocket(t.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0;
const pending = new Map();
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pending.has(d.id)) { const { res, rej } = pending.get(d.id); pending.delete(d.id); d.error ? rej(new Error(d.error.message)) : res(d.result); }
};
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); });
async function evaluate(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error("eval failed: " + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
}
async function waitFor(expr, timeout = 30000, label = expr) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try { const v = await evaluate(expr); if (v) return v; } catch { /* navigating */ }
    await sleep(150);
  }
  throw new Error("timeout waiting for: " + label);
}
async function click(js, label = js) {
  const box = await waitFor(`(() => { const el = (${js}); if (!el) return null; el.scrollIntoView({block: "center"}); const r = el.getBoundingClientRect(); return r.width ? {x: r.x + r.width/2, y: r.y + r.height/2} : null; })()`, 15000, label);
  await sleep(80);
  for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
}
async function shot(name) {
  await sleep(300);
  const r = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(SHOTS, name + ".png"), Buffer.from(r.data, "base64"));
  console.log("screenshot -> " + name);
}
const text = (sel) => `(document.querySelector(${JSON.stringify(sel)})?.innerText || "")`;
async function openProject(path) {
  await send("Page.navigate", { url: URL0 });
  await waitFor(`document.readyState === "complete" && !!document.querySelector("#folder-path")`, 30000, "home");
  await evaluate(`document.querySelector("#folder-path").select()`);
  await send("Input.insertText", { text: path });
  const base = { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 };
  await send("Input.dispatchKeyEvent", { type: "keyDown", ...base, text: "\r", unmodifiedText: "\r" });
  await send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
  await waitFor(`${text(".proj-actions .pill")}.trim() === "Ready"`, 180000, "Ready");
}

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 860, deviceScaleFactor: 2, mobile: false });
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }, { name: "prefers-reduced-motion", value: "reduce" }] });

if (STEP === "reconnect") {
  await openProject(process.env.SAMPLE);
  process.kill(Number(process.env.ENGINE_PID), "SIGTERM");
  await waitFor(`/Reconnecting/.test(document.body.innerText)`, 30000, "reconnecting indicator");
  await evaluate(`document.activeElement && document.activeElement.blur()`);
  await click(`document.querySelector(".ask-input")`);
  await shot("16-reconnecting");
} else if (STEP === "stale") {
  const stale = process.env.STALE;
  await openProject(stale);
  await click(`document.querySelector(".ask-input")`);
  await evaluate(`document.querySelector(".ask-input").select()`);
  await send("Input.insertText", { text: "Explain how GPS data travels through this application." });
  const base = { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 };
  await send("Input.dispatchKeyEvent", { type: "keyDown", ...base, text: "\r", unmodifiedText: "\r" });
  await send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
  await waitFor(`!!document.querySelector(".answer .flow")`, 180000, "answer");
  appendFileSync(join(stale, "nav/navigation.c"), "\n/* edited after indexing */\n");      // line numbers stay valid
  await evaluate(`window.dispatchEvent(new Event("focus"))`);
  await waitFor(`/1 file changed/.test(${text(".proj-actions")})`, 30000, "stale status");
  await evaluate(`window.scrollTo(0, 0)`);
  await shot("17-stale-files");
  await click(`[...document.querySelectorAll(".answer a, .answer button")].find(e => e.innerText.trim() === "nav/navigation.c:40")`, "citation nav/navigation.c:40");
  await waitFor(`/has changed since/.test(document.body.innerText)`, 30000, "stale warning in the source viewer");
  await evaluate(`window.scrollTo(0, 0)`);
  await shot("18-source-stale");
}
await send("Target.closeTarget", { targetId: t.id }).catch(() => {});
ws.close();
process.exit(0);
