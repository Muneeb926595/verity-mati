// Connection to the local engine: JSON-RPC over POST /rpc and one Server-Sent Events stream (docs/ENGINE_API.md).
import { batch, createSignal } from "solid-js";
import { createStore, produce } from "solid-js/store";
import type { AskProgressEvent, IndexProgressEvent, Job, JobState } from "./types";

// ------------------------------------------------------------------------------------------ where the engine is
interface Injected {
  port?: number | string;
  token?: string;
}

function resolveConnection(): { base: string; token: string } {
  const inj = (window as unknown as { __VERITY__?: Injected }).__VERITY__;
  if (inj && inj.port && inj.token) {
    return { base: `http://127.0.0.1:${inj.port}`, token: String(inj.token) };
  }
  const q = new URLSearchParams(location.search);
  return { base: location.origin, token: q.get("token") || "" };
}

export const conn = resolveConnection();

// ------------------------------------------------------------------------------------------ RPC
export class RpcError extends Error {
  code: number;
  data?: unknown;
  /** true when the engine could not be reached at all (still starting, or stopped) */
  network: boolean;
  constructor(message: string, code = -32000, data?: unknown, network = false) {
    super(message);
    this.code = code;
    this.data = data;
    this.network = network;
  }
}

export const UNAUTHORIZED = -32003;

/** false while the engine cannot be reached; drives the "Starting the engine…" / reconnecting UI. */
export const [engineReachable, setEngineReachable] = createSignal(true);

let nextId = 1;

export async function rpc<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(conn.base + "/rpc", {
      method: "POST",
      headers: { Authorization: "Bearer " + conn.token, "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: nextId++, method, params }),
      cache: "no-store",
    });
  } catch {
    setEngineReachable(false);
    throw new RpcError("Can't reach the Verity engine right now. It may still be starting.", -1, undefined, true);
  }
  setEngineReachable(true);
  let body: any = null;
  try {
    body = await res.json();
  } catch {
    if (res.status === 403) {
      throw new RpcError("The engine refused this page. Open Verity from the address the engine printed.", 403);
    }
    throw new RpcError(`The engine sent an unexpected reply (HTTP ${res.status}).`, res.status);
  }
  if (res.status === 401 || body?.error?.code === UNAUTHORIZED) {
    throw new RpcError(
      "This page is missing its access key. Open Verity from the address the engine printed (it ends in ?token=…).",
      UNAUTHORIZED,
    );
  }
  if (body?.error) {
    throw new RpcError(String(body.error.message || "Something went wrong."), Number(body.error.code) || -32000, body.error.data);
  }
  return body?.result as T;
}

/** Engine liveness without auth (GET /health). */
export async function ping(): Promise<boolean> {
  try {
    const r = await fetch(conn.base + "/health", { cache: "no-store" });
    return r.ok;
  } catch {
    return false;
  }
}

export function errorText(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

// ------------------------------------------------------------------------------------------ events (SSE)
export type SseState = "connecting" | "open" | "reconnecting";
export const [sseState, setSseState] = createSignal<SseState>("connecting");

type EngineEvent = { type: string; [k: string]: any };
type Listener = (ev: EngineEvent) => void;

const listeners = new Set<Listener>();
const reconnectHooks = new Set<() => void>();
const EVENT_TYPES = ["index.progress", "ask.progress", "job.queued", "job.done", "job.failed", "job.cancelled", "job.cancelling"];

let source: EventSource | null = null;
let retryMs = 500;
let retryTimer: number | undefined;
let everOpened = false;

export function onEngineEvent(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Called after the event stream comes back, so views can re-sync anything they missed. */
export function onReconnect(fn: () => void): () => void {
  reconnectHooks.add(fn);
  return () => reconnectHooks.delete(fn);
}

function dispatch(ev: EngineEvent) {
  for (const fn of listeners) {
    try {
      fn(ev);
    } catch (err) {
      console.error("event handler failed", err);
    }
  }
}

export function connectEvents() {
  if (source) source.close();
  clearTimeout(retryTimer);
  const es = new EventSource(conn.base + "/events?token=" + encodeURIComponent(conn.token));
  source = es;
  es.onopen = () => {
    retryMs = 500;
    const wasOpen = everOpened;
    everOpened = true;
    setSseState("open");
    setEngineReachable(true);
    if (wasOpen) reconnectHooks.forEach((fn) => fn());
  };
  es.onerror = () => {
    if (source !== es) return;
    setSseState(everOpened ? "reconnecting" : "connecting");
    if (es.readyState === EventSource.CLOSED) {
      // the browser gave up (e.g. the engine restarted); retry ourselves with a gentle backoff
      es.close();
      source = null;
      retryTimer = window.setTimeout(connectEvents, retryMs);
      retryMs = Math.min(retryMs * 2, 5000);
    }
  };
  for (const t of EVENT_TYPES) {
    es.addEventListener(t, (m: MessageEvent) => {
      let data: EngineEvent;
      try {
        data = JSON.parse(m.data);
      } catch {
        return;
      }
      if (!data.type) data.type = t;
      dispatch(data);
    });
  }
}

// ------------------------------------------------------------------------------------------ jobs
export const PHASES: Array<{ key: string; label: string }> = [
  { key: "scan", label: "Looking at your project files" },
  { key: "plan", label: "Working out how the code is built" },
  { key: "c", label: "Reading the C code" },
  { key: "cs", label: "Reading the C# code" },
  { key: "check", label: "Double-checking with a second parser" },
  { key: "link", label: "Connecting the pieces together" },
  { key: "search", label: "Preparing search" },
];

export interface PhaseView {
  state: "pending" | "running" | "done" | "skipped" | "failed";
  label: string;
  detail: string;
}

export interface JobView {
  id: string;
  kind: string;
  project: string;
  state: JobState;
  cancelling: boolean;
  error: string | null;
  result: any;
  /** latest index.progress for this job */
  index: IndexProgressEvent | null;
  phases: Record<string, PhaseView>;
  languages: string[] | null;
  /** latest ask.progress for this job */
  ask: AskProgressEvent | null;
  lastEvent: number;
}

const [jobs, setJobs] = createStore<Record<string, JobView>>({});
export { jobs };

const TERMINAL: JobState[] = ["done", "failed", "cancelled"];
const RANK: Record<string, number> = { queued: 0, running: 1, done: 2, failed: 2, cancelled: 2 };
export const isTerminal = (s: JobState | undefined) => !!s && TERMINAL.includes(s);

const waiters = new Map<string, Array<(j: JobView) => void>>();
const pollers = new Map<string, number>();

function blankPhases(): Record<string, PhaseView> {
  const out: Record<string, PhaseView> = {};
  for (const p of PHASES) out[p.key] = { state: "pending", label: p.label, detail: "" };
  return out;
}

function ensureJob(j: Partial<Job> & { id: string }) {
  if (!jobs[j.id]) {
    setJobs(j.id, {
      id: j.id,
      kind: j.kind || "",
      project: j.project || "",
      state: (j.state as JobState) || "queued",
      cancelling: false,
      error: null,
      result: null,
      index: null,
      phases: blankPhases(),
      languages: null,
      ask: null,
      lastEvent: Date.now(),
    });
  }
}

function settle(id: string) {
  const j = jobs[id];
  if (!j || !isTerminal(j.state)) return;
  const t = pollers.get(id);
  if (t) {
    clearInterval(t);
    pollers.delete(id);
  }
  const ws = waiters.get(id);
  if (ws) {
    waiters.delete(id);
    ws.forEach((fn) => fn(j));
  }
}

function applyJobObject(j: Job, withResult: boolean) {
  ensureJob(j);
  setJobs(
    j.id,
    produce((v) => {
      v.kind = j.kind || v.kind;
      v.project = j.project || v.project;
      // never move a job backwards (queued → running → finished) because of a late or stale message
      if (RANK[j.state] >= RANK[v.state] && !isTerminal(v.state)) v.state = j.state;
      if (j.error) v.error = j.error;
      if (withResult && j.result !== undefined) v.result = j.result;
      v.lastEvent = Date.now();
    }),
  );
}

function applyIndexProgress(ev: IndexProgressEvent) {
  ensureJob({ id: ev.job, kind: "index", project: ev.project, state: "running" });
  setJobs(
    ev.job,
    produce((v) => {
      if (!isTerminal(v.state)) v.state = "running";
      v.index = ev;
      v.lastEvent = Date.now();
      const idx = PHASES.findIndex((p) => p.key === ev.phase);
      const ph = v.phases[ev.phase] || (v.phases[ev.phase] = { state: "pending", label: ev.label, detail: "" });
      ph.state = ev.state;
      ph.label = ev.label || ph.label;
      if (ev.detail) ph.detail = ev.detail;
      // phases arrive in order: anything before the current one that is still "pending"/"running" is finished
      if (idx > 0) {
        for (let i = 0; i < idx; i++) {
          const prev = v.phases[PHASES[i].key];
          if (prev && (prev.state === "pending" || prev.state === "running")) prev.state = "done";
        }
      }
      if (ev.languages && ev.languages.length) v.languages = ev.languages;
    }),
  );
}

// index progress can be very chatty on big projects: coalesce into one update per animation frame
let pendingIndex: IndexProgressEvent[] = [];
let frameQueued = false;
function queueIndexProgress(ev: IndexProgressEvent) {
  pendingIndex.push(ev);
  if (frameQueued) return;
  frameQueued = true;
  const flush = () => {
    frameQueued = false;
    const evs = pendingIndex;
    pendingIndex = [];
    batch(() => evs.forEach(applyIndexProgress));
  };
  // rAF does not fire in background tabs; fall back to a timer so progress still lands
  if (document.visibilityState === "visible") requestAnimationFrame(flush);
  else setTimeout(flush, 100);
}

onEngineEvent((ev) => {
  switch (ev.type) {
    case "index.progress":
      queueIndexProgress(ev as IndexProgressEvent);
      break;
    case "ask.progress": {
      const a = ev as AskProgressEvent;
      ensureJob({ id: a.job, kind: "ask", project: a.project, state: "running" });
      setJobs(
        a.job,
        produce((v) => {
          if (!isTerminal(v.state)) v.state = "running";
          v.ask = a;
          v.lastEvent = Date.now();
        }),
      );
      break;
    }
    case "job.queued":
    case "job.done":
    case "job.failed":
    case "job.cancelled":
    case "job.cancelling": {
      const j = ev.job as Job;
      if (!j || !j.id) return;
      if (ev.type === "job.done" && j.kind === "index") {
        // make sure every queued progress update lands before the job is marked finished
        const evs = pendingIndex;
        pendingIndex = [];
        batch(() => evs.forEach(applyIndexProgress));
      }
      applyJobObject(j, true);
      if (ev.type === "job.cancelling") setJobs(j.id, "cancelling", true);
      settle(j.id);
      break;
    }
  }
});

/** Start following a job returned by `index.start` / `ask`. Polls as a safety net if events go quiet. */
export function trackJob(j: Job): JobView {
  applyJobObject(j, false);
  settle(j.id);
  if (!isTerminal(jobs[j.id].state) && !pollers.has(j.id)) {
    const t = window.setInterval(async () => {
      const v = jobs[j.id];
      if (!v || isTerminal(v.state)) return settle(j.id);
      if (Date.now() - v.lastEvent < 4000 && sseState() === "open") return;
      try {
        const fresh = await rpc<Job>("job.get", { job: j.id, with_result: true });
        applyJobObject(fresh, true);
        if (fresh.progress && fresh.kind === "index" && fresh.progress.phase) {
          applyIndexProgress({ ...fresh.progress, type: "index.progress", job: fresh.id, project: fresh.project });
        }
        settle(j.id);
      } catch (e) {
        if (e instanceof RpcError && !e.network && e.code !== UNAUTHORIZED) {
          // the engine forgot the job (e.g. it restarted): treat it as failed so the UI does not hang
          setJobs(j.id, produce((v) => { v.state = "failed"; v.error = e.message; }));
          settle(j.id);
        }
      }
    }, 2500);
    pollers.set(j.id, t);
  }
  return jobs[j.id];
}

/** Resolves once the job is done, failed or cancelled. */
export function waitJob(id: string): Promise<JobView> {
  const j = jobs[id];
  if (j && isTerminal(j.state)) return Promise.resolve(j);
  return new Promise((resolve) => {
    const list = waiters.get(id) || [];
    list.push(resolve);
    waiters.set(id, list);
  });
}

export async function cancelJob(id: string) {
  setJobs(id, "cancelling", true);
  try {
    await rpc("job.cancel", { job: id });
  } catch (e) {
    setJobs(id, "cancelling", false);
    throw e;
  }
}
