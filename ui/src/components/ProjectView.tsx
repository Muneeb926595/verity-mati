import { createMemo, createSignal, For, onCleanup, onMount, Show } from "solid-js";
import {
  cancelJob,
  errorText,
  isTerminal,
  jobs,
  onEngineEvent,
  onReconnect,
  PHASES,
  rpc,
  trackJob,
  waitJob,
  type JobView,
} from "../api";
import { goHome, takeOpened } from "../router";
import type { Answer, CorrectResult, HistoryItem, Job, LearningListing, ProjectStatus, SourceTarget } from "../types";
import { etaText, languagesLabel, plural, shortPath, timeAgo } from "../util";
import AnswerView, { type LearnHandlers } from "./AnswerView";
import { Banner, SourceCtx, Spinner } from "./common";
import LearningPanel from "./LearningPanel";
import SourceViewer, { type PickedLine } from "./SourceViewer";

interface Notice {
  kind: "error" | "warn" | "info" | "ok";
  text: string;
  details?: string | null;
}

interface Asking {
  question: string;
  started: number;
  jobId: string | null;
}

const smooth = (): ScrollBehavior =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";

function splitError(err: string | null | undefined): { text: string; details: string | null } {
  const s = (err || "").trim();
  if (!s) return { text: "Something went wrong.", details: null };
  const nl = s.indexOf("\n");
  if (nl < 0) return { text: s, details: null };
  return { text: s.slice(0, nl), details: s.slice(nl + 1) };
}

export default function ProjectView(props: { path: string }) {
  const [status, setStatus] = createSignal<ProjectStatus | null>(null);
  const [loadError, setLoadError] = createSignal<string | null>(null);
  const [indexJobId, setIndexJobId] = createSignal<string | null>(null);
  const [indexNotice, setIndexNotice] = createSignal<Notice | null>(null);
  const [question, setQuestion] = createSignal("");
  const [asking, setAsking] = createSignal<Asking | null>(null);
  const [askNotice, setAskNotice] = createSignal<Notice | null>(null);
  const [answer, setAnswer] = createSignal<Answer | null>(null);
  const [history, setHistory] = createSignal<HistoryItem[]>([]);
  const [source, setSource] = createSignal<SourceTarget | null>(null);
  // learning from corrections
  const [learnBusy, setLearnBusy] = createSignal("");
  const [learnDone, setLearnDone] = createSignal<Record<string, { kind: "corrected" | "confirmed"; text: string }>>({});
  const [learnNotice, setLearnNotice] = createSignal<{ kind: "ok" | "error" | "info"; text: string; undo?: string; question?: string } | null>(null);
  const [pickError, setPickError] = createSignal<string | null>(null);
  const [learnCounts, setLearnCounts] = createSignal<LearningListing["counts"] | null>(null);
  const [learnOpen, setLearnOpen] = createSignal(false);
  let pickFor: Answer | null = null;
  const [exampleFns, setExampleFns] = createSignal<string[]>([]);
  const [now, setNow] = createSignal(Date.now());
  let askInput: HTMLInputElement | undefined;
  let answerTop: HTMLDivElement | undefined;
  let disposed = false;
  onCleanup(() => (disposed = true));

  const path = () => status()?.path || props.path;
  const indexJob = (): JobView | null => {
    const id = indexJobId();
    return id ? jobs[id] || null : null;
  };
  const indexing = () => {
    const j = indexJob();
    return !!j && !isTerminal(j.state);
  };
  const askJob = (): JobView | null => {
    const a = asking();
    return a && a.jobId ? jobs[a.jobId] || null : null;
  };
  const canAsk = () => !!status()?.ready;

  // ---------------------------------------------------------------------------------------- open + index
  async function open() {
    setLoadError(null);
    let st = takeOpened(props.path);
    try {
      if (!st) st = await rpc<ProjectStatus>("project.open", { path: props.path });
    } catch (e) {
      setLoadError(errorText(e));
      return;
    }
    if (disposed) return;
    setStatus(st);
    document.title = `${st.name} — Verity`;
    const running = (st.jobs || []).find((j) => j.kind === "index" && !isTerminal(j.state));
    if (running) {
      attachIndex(running);
    } else if (!st.ready || st.state !== "complete" || (st.stale && st.stale.length)) {
      startIndex(false);
    } else {
      focusAsk();
    }
    loadHistory();
    loadExamples();
    loadLearnCounts();
  }

  async function refreshStatus() {
    try {
      const st = await rpc<ProjectStatus>("project.status", { path: path() });
      if (disposed) return;
      setStatus(st);
      const running = (st.jobs || []).find((j) => j.kind === "index" && !isTerminal(j.state));
      if (running && running.id !== indexJobId()) attachIndex(running);
    } catch {
      /* keep what we have; the reconnect indicator already tells the user */
    }
  }

  async function startIndex(full: boolean) {
    setIndexNotice(null);
    try {
      const job = await rpc<Job>("index.start", { path: path(), full });
      attachIndex(job);
    } catch (e) {
      setIndexNotice({ kind: "error", text: errorText(e) });
    }
  }

  async function attachIndex(job: Job) {
    const known = jobs[job.id];
    if (indexJobId() === job.id && known && !isTerminal(known.state)) return; // already following it
    setIndexJobId(job.id);
    trackJob(job);
    setStatus((s) => (s ? { ...s, label: "Indexing…" } : s));
    const final = await waitJob(job.id);
    if (disposed || indexJobId() !== job.id) return;
    await refreshStatus();
    if (final.state === "done") {
      const st = status();
      setIndexNotice(
        st && st.stale && st.stale.length
          ? { kind: "warn", text: `Finished, but ${plural(st.stale.length, "file")} changed while reading. Press Update to catch up.` }
          : null,
      );
      loadExamples();
      loadHistory();
      focusAsk();
    } else if (final.state === "cancelled") {
      setIndexNotice({ kind: "info", text: "Stopped. Press Update whenever you want Verity to finish reading the project." });
    } else {
      const { text, details } = splitError(final.error);
      setIndexNotice({ kind: "error", text: "Reading the project failed: " + text, details });
    }
  }

  // ---------------------------------------------------------------------------------------- questions
  // ---------------------------------------------------------------------------------------- learning
  async function loadLearnCounts() {
    try {
      setLearnCounts((await rpc<{ counts: LearningListing["counts"] }>("corrections.summary", { path: path() })).counts);
    } catch {
      /* not essential */
    }
  }

  /** After a correction or an undo: ask the same question again, so the answer shown is the one Verity now gives. */
  function reask(q: string) {
    if (q && canAsk()) ask(q);
  }

  async function correctAnswer(a: Answer, args: { pick?: number; target?: { file: string; line: number }; note?: string }) {
    if (!a.id || learnBusy()) return;
    setLearnBusy("Saving…");
    setPickError(null);
    try {
      const r = await rpc<CorrectResult>("answer.correct", { path: path(), answer_id: a.id, ...args });
      setLearnDone({ ...learnDone(), [a.id]: { kind: "corrected", text: r.message } });
      const terms = (r.glossary_suggestions || []).map((t) => `“${t.term}”`);
      setLearnNotice({
        kind: "ok",
        text: r.message + (terms.length ? ` It also suggests the glossary word${terms.length > 1 ? "s" : ""} ${terms.join(", ")} (see Learned).` : ""),
        undo: r.correction.id,
        question: a.question,
      });
      setSource(null);
      pickFor = null;
      loadLearnCounts();
      reask(a.question);
    } catch (e) {
      if (args.target) setPickError(errorText(e));
      else setLearnNotice({ kind: "error", text: errorText(e) });
    } finally {
      setLearnBusy("");
    }
  }

  async function confirmAnswer(a: Answer) {
    if (!a.id || learnBusy()) return;
    setLearnBusy("Saving…");
    try {
      const r = await rpc<{ message: string }>("answer.confirm", { path: path(), answer_id: a.id });
      setLearnDone({ ...learnDone(), [a.id]: { kind: "confirmed", text: r.message } });
      loadLearnCounts();
    } catch (e) {
      setLearnNotice({ kind: "error", text: errorText(e) });
    } finally {
      setLearnBusy("");
    }
  }

  async function undoCorrection(id: string, question?: string) {
    if (learnBusy()) return;
    setLearnBusy("Undoing…");
    try {
      const r = await rpc<{ message: string }>("corrections.remove", { path: path(), id });
      setLearnNotice({ kind: "info", text: r.message });
      loadLearnCounts();
      if (question) reask(question);
    } catch (e) {
      setLearnNotice({ kind: "error", text: errorText(e) });
    } finally {
      setLearnBusy("");
    }
  }

  function openPick(a: Answer) {
    const c = a.meta?.learning?.candidates?.[0];
    const ev = (a.evidence || []).find((e) => e.file);
    const file = c?.file || ev?.file;
    if (!file || !a.id) return;
    pickFor = a;
    setPickError(null);
    const files = [...new Set([...(a.meta?.learning?.candidates || []).map((x) => x.file), ...(a.evidence || []).map((e) => e.file)]
      .filter((f): f is string => !!f))];
    setSource({ file, line: c?.line || ev?.line || 1, pick: { answerId: a.id, question: a.question, files } });
  }

  function onPicked(sel: PickedLine) {
    if (pickFor) correctAnswer(pickFor, { target: { file: sel.file, line: sel.line }, note: sel.note });
  }

  const learnHandlers = (a: Answer): LearnHandlers => ({
    pick: (rank) => correctAnswer(a, { pick: rank }),
    confirm: () => confirmAnswer(a),
    pickLine: () => openPick(a),
    undo: (id) => undoCorrection(id, a.question),
    busy: () => learnBusy(),
    done: () => (a.id ? learnDone()[a.id] || null : null),
  });

  async function loadHistory() {
    try {
      setHistory(await rpc<HistoryItem[]>("history.list", { path: path(), limit: 20 }));
    } catch {
      /* optional */
    }
  }

  async function loadExamples() {
    if (!status()?.ready || exampleFns().length) return;
    try {
      const hits = await rpc<Array<{ name: string; kind: string; file?: string }>>("search", {
        path: path(),
        query: "process handle update data input",
        k: 20,
      });
      const names: string[] = [];
      for (const h of hits || []) {
        if (!["function", "method"].includes(h.kind)) continue;
        if (!h.file || /\.(h|hh|hpp|hxx)$/i.test(h.file)) continue;
        if (/^(get_|set_|add_|remove_)|^(main|Main)$|^[_.]/.test(h.name) || h.name.includes("<") || h.name.length > 40) continue;
        if (!names.includes(h.name)) names.push(h.name);
        if (names.length >= 2) break;
      }
      if (!disposed) setExampleFns(names);
    } catch {
      /* generic examples are fine */
    }
  }

  function focusAsk() {
    queueMicrotask(() => askInput?.focus({ preventScroll: true }));
  }

  async function ask(q: string) {
    q = q.trim();
    if (!q || asking() || !canAsk()) return;
    if (/<[^>]+>/.test(q)) {
      setAskNotice({ kind: "info", text: "Replace the part in <angle brackets> with a name from your code first." });
      selectPlaceholder();
      return;
    }
    setAskNotice(null);
    setQuestion(q);
    const started = Date.now();
    setAsking({ question: q, started, jobId: null });
    setNow(started);
    const tick = window.setInterval(() => setNow(Date.now()), 250);
    try {
      const job = await rpc<Job>("ask", { path: path(), question: q });
      setAsking({ question: q, started, jobId: job.id });
      trackJob(job);
      const final = await waitJob(job.id);
      if (disposed) return;
      if (final.state === "done" && final.result) {
        setAnswer(final.result as Answer);
        loadHistory();
        queueMicrotask(() => answerTop?.scrollIntoView({ behavior: smooth(), block: "start" }));
      } else if (final.state === "cancelled") {
        setAskNotice({ kind: "info", text: "Stopped. Ask again whenever you like." });
      } else {
        const { text, details } = splitError(final.error || "The question could not be answered.");
        setAskNotice({ kind: "error", text, details });
      }
    } catch (e) {
      setAskNotice({ kind: "error", text: errorText(e) });
    } finally {
      clearInterval(tick);
      setAsking(null);
    }
  }

  async function cancelAsk() {
    const a = asking();
    if (!a || !a.jobId) return;
    try {
      await cancelJob(a.jobId);
    } catch (e) {
      setAskNotice({ kind: "error", text: errorText(e) });
    }
  }

  async function showStored(item: HistoryItem) {
    setAskNotice(null);
    try {
      const a = await rpc<Answer>("answer.get", { path: path(), id: item.id });
      setAnswer(a);
      setQuestion(a.question || item.question);
      queueMicrotask(() => answerTop?.scrollIntoView({ behavior: smooth(), block: "start" }));
    } catch (e) {
      setAskNotice({ kind: "error", text: errorText(e) });
    }
  }

  function selectPlaceholder() {
    queueMicrotask(() => {
      if (!askInput) return;
      askInput.focus();
      const v = askInput.value;
      const a = v.indexOf("<");
      const b = v.indexOf(">", a);
      if (a >= 0 && b > a) askInput.setSelectionRange(a, b + 1);
    });
  }

  function useExample(q: string) {
    if (/<[^>]+>/.test(q)) {
      setQuestion(q);
      selectPlaceholder();
    } else {
      ask(q);
    }
  }

  const examples = createMemo(() => {
    const [f1, f2] = exampleFns();
    const fn = (n: string | undefined) => (n ? `${n}()` : "<function name>");
    return [
      "Explain how data travels through this application.",
      `What does ${fn(f1)} do?`,
      "What happens if the input becomes unavailable?",
      `What could be affected if I change ${fn(f2 || f1)}?`,
      `Who calls ${fn(f1)}?`,
      "What happens if the input data is invalid?",
    ];
  });

  // ---------------------------------------------------------------------------------------- lifecycle
  onMount(() => {
    open();
    let lastFocus = 0;
    const onFocus = () => {
      // pick up files edited outside Verity ("Ready — 3 files changed")
      if (Date.now() - lastFocus < 5000 || indexing() || !status()) return;
      lastFocus = Date.now();
      refreshStatus();
    };
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
      if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey && canAsk()) {
        e.preventDefault();
        askInput?.focus();
      }
    };
    window.addEventListener("focus", onFocus);
    window.addEventListener("keydown", onKey);
    onCleanup(() => {
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("keydown", onKey);
    });
  });
  onCleanup(onReconnect(() => status() && refreshStatus()));
  // an index started elsewhere (another window, the CLI, the MCP server) shows up here too
  onCleanup(
    onEngineEvent((ev) => {
      if (!status() || indexing()) return;
      const j = ev.type === "job.queued" ? (ev.job as Job) : null;
      if (j && j.kind === "index" && j.project === path()) attachIndex(j);
      else if (ev.type === "index.progress" && ev.project === path() && ev.job !== indexJobId()) {
        attachIndex({ id: ev.job, kind: "index", project: ev.project, state: "running" });
      }
    }),
  );
  onCleanup(() => {
    document.title = "Verity";
  });

  // ---------------------------------------------------------------------------------------- status pill
  const pill = createMemo(() => {
    const st = status();
    const j = indexJob();
    if (indexing()) {
      return { cls: "amber", spin: true, text: j?.cancelling ? "Stopping…" : "Indexing…", update: false };
    }
    if (!st) return { cls: "gray", spin: true, text: "Opening…", update: false };
    const stale = st.stale?.length || 0;
    if (st.state === "complete" && !stale) return { cls: "green", dot: true, text: "Ready", update: false };
    if (st.state === "complete") return { cls: "amber", text: `Ready — ${plural(stale, "file")} changed`, update: true };
    if (st.state === "new") return { cls: "gray", text: "Not indexed yet", update: true };
    if (st.state === "failed") return { cls: "red", text: "Indexing failed", update: true, title: st.label };
    return { cls: "amber", text: "Not finished", update: true, title: st.label };
  });

  const languages = () => indexJob()?.languages || status()?.languages || [];

  return (
    <SourceCtx.Provider value={{ open: (t) => setSource({ ...t }) }}>
      <div class="project" classList={{ "with-panel": !!source() }}>
        <main class="content">
          <Show
            when={!loadError()}
            fallback={
              <div class="center-screen">
                <div class="big-icon" aria-hidden="true">!</div>
                <h2>This project could not be opened</h2>
                <p class="muted narrow">{loadError()}</p>
                <div class="row gap">
                  <button class="btn" onClick={open}>
                    Try again
                  </button>
                  <button class="btn primary" onClick={goHome}>
                    Back to projects
                  </button>
                </div>
              </div>
            }
          >
            {/* ------------------------------------------------------------------ header */}
            <div class="proj-head">
              <div class="proj-title">
                <h1>{status()?.name || props.path.split("/").filter(Boolean).pop()}</h1>
                <div class="proj-sub">
                  <span class="proj-path" title={path()}>
                    {shortPath(path())}
                  </span>
                  <Show when={languagesLabel(languages())}>
                    <span class="lang-tag">{languagesLabel(languages())}</span>
                  </Show>
                </div>
              </div>
              <div class="proj-actions">
                <span class={`pill ${pill().cls}`} role="status" title={(pill() as { title?: string }).title || undefined}>
                  <Show when={pill().spin}>
                    <Spinner />
                  </Show>
                  <Show when={pill().dot}>
                    <span class="pill-dot" aria-hidden="true" />
                  </Show>
                  {pill().text}
                </span>
                <Show when={pill().update}>
                  <button class="btn sm" onClick={() => startIndex(false)} title="Read the project again (only changed files)">
                    {status()?.state === "new" ? "Start" : "Update"}
                  </button>
                </Show>
                <button class="btn sm subtle learn-btn" onClick={() => setLearnOpen(true)}
                  title="Corrections and glossary words Verity learned for this project">
                  Learned
                  <Show when={learnCounts() && (learnCounts()!.active || learnCounts()!.suggested)}>
                    <span class="count">{learnCounts()!.active || ""}{learnCounts()!.suggested ? (learnCounts()!.active ? " · " : "") + `${learnCounts()!.suggested} new` : ""}</span>
                  </Show>
                  <Show when={learnCounts()?.stale}>
                    <span class="stale-dot" title={`${learnCounts()!.stale} stale`} aria-label={`${learnCounts()!.stale} stale`} />
                  </Show>
                </button>
                <HistoryMenu items={history()} onPick={showStored} />
              </div>
            </div>

            <Show when={indexNotice()}>
              {(n) => (
                <Banner kind={n().kind} details={n().details} onClose={() => setIndexNotice(null)}>
                  {n().text}
                </Banner>
              )}
            </Show>

            {/* ------------------------------------------------------------------ indexing */}
            <Show when={indexing() && indexJob()}>
              {(j) => <IndexCard job={j()} languages={languages()} onCancel={() => cancelJob(j().id).catch((e) => setIndexNotice({ kind: "error", text: errorText(e) }))} />}
            </Show>

            {/* ------------------------------------------------------------------ question box */}
            <section class="ask-section" aria-label="Ask a question">
              <form
                class="ask-box"
                classList={{ disabled: !canAsk() }}
                onSubmit={(e) => {
                  e.preventDefault();
                  ask(question());
                }}
              >
                <input
                  ref={askInput}
                  class="ask-input"
                  type="text"
                  placeholder={canAsk() ? "Ask anything about your code…" : "You can ask questions as soon as Verity has read your project…"}
                  aria-label="Your question"
                  autocomplete="off"
                  spellcheck={false}
                  maxLength={2000}
                  value={question()}
                  disabled={!canAsk()}
                  onInput={(e) => setQuestion(e.currentTarget.value)}
                  onKeyDown={(e) => {
                    // Enter submits (explicitly, so it also works where implicit form submission does not)
                    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
                      e.preventDefault();
                      ask(e.currentTarget.value);
                    }
                  }}
                />
                <button class="btn primary ask-btn" type="submit" disabled={!canAsk() || !!asking() || !question().trim()}>
                  Ask
                </button>
              </form>
              <Show when={canAsk() && !asking()}>
                <div class="chips" aria-label="Example questions">
                  <For each={examples()}>
                    {(q) => (
                      <button class="chip" onClick={() => useExample(q)} title={/<[^>]+>/.test(q) ? "Fill in the name, then press Enter" : "Ask this"}>
                        {q}
                      </button>
                    )}
                  </For>
                </div>
              </Show>

              <Show when={asking()}>
                {(a) => <AskProgress asking={a()} job={askJob()} now={now()} onCancel={cancelAsk} />}
              </Show>
              <Show when={askNotice()}>
                {(n) => (
                  <Banner kind={n().kind} details={n().details} onClose={() => setAskNotice(null)}>
                    {n().text}
                  </Banner>
                )}
              </Show>
            </section>

            {/* ------------------------------------------------------------------ answer */}
            <div ref={answerTop} class="answer-anchor" />
            <Show when={learnNotice()}>
              {(n) => (
                <Banner kind={n().kind} onClose={() => setLearnNotice(null)}
                  action={n().undo ? (
                    <button class="btn sm" disabled={!!learnBusy()} onClick={() => { const u = n().undo!; const q = n().question; setLearnNotice(null); undoCorrection(u, q); }}>
                      Undo
                    </button>
                  ) : undefined}>
                  {n().text}
                </Banner>
              )}
            </Show>
            <div classList={{ "answer-wrap": true, dimmed: !!asking() }} aria-busy={!!asking()}>
              <Show when={answer()} keyed>
                {(a) => <AnswerView answer={a} learn={a.id ? learnHandlers(a) : undefined} />}
              </Show>
            </div>
          </Show>
        </main>

        <Show when={source()} keyed>
          {(t) => (
            <SourceViewer path={path()} target={t} onClose={() => { setSource(null); setPickError(null); pickFor = null; }}
              onPicked={onPicked} pickBusy={learnBusy()} pickError={pickError()} />
          )}
        </Show>
        <Show when={learnOpen()}>
          <LearningPanel path={path()} name={status()?.name || ""} onClose={() => setLearnOpen(false)}
            onOpenSource={(t) => { setLearnOpen(false); setSource({ ...t }); }}
            onAsk={(q) => { setQuestion(q); ask(q); }}
            onChanged={loadLearnCounts} />
        </Show>
      </div>
    </SourceCtx.Provider>
  );
}

// ------------------------------------------------------------------------------------------ indexing card
function IndexCard(props: { job: JobView; languages: string[]; onCancel: () => void }) {
  const [details, setDetails] = createSignal(false);
  const ev = () => props.job.index;
  const total = () => ev()?.phase_total || PHASES.length;
  const completed = () => {
    const e = ev();
    if (!e) return 0;
    return Math.max(0, e.phase_index - (e.state === "running" ? 1 : 0));
  };
  const fraction = () => {
    const e = ev();
    if (!e) return 0.02;
    let c = completed();
    if (e.state === "running" && e.done != null && e.total) c += Math.min(1, e.done / e.total);
    return Math.max(0.02, Math.min(1, c / total()));
  };
  const langs = () => languagesLabel(props.job.languages || props.languages);
  const headline = () => {
    if (props.job.cancelling) return "Stopping…";
    if (!ev()) return props.job.state === "queued" ? "Getting ready to read your project…" : "Starting…";
    return `Understanding your ${langs() ? langs() + " " : ""}project… ${completed()} of ${total()} parts complete`;
  };
  const counter = () => {
    const e = ev();
    if (!e || e.state !== "running" || e.done == null || !e.total) return "";
    return `${e.done} / ${e.total}`;
  };
  const eta = () => {
    const e = ev();
    return e && e.state === "running" ? etaText(e.eta_s) : "";
  };

  return (
    <section class="card index-card" aria-live="polite">
      <div class="index-top">
        <div class="index-head">
          <Spinner size="md" />
          <h2 class="index-headline">{headline()}</h2>
        </div>
        <button class="btn subtle" onClick={props.onCancel} disabled={props.job.cancelling}>
          Cancel
        </button>
      </div>
      <div
        class="bar"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(fraction() * 100)}
        aria-label="Progress"
      >
        <div class="bar-fill" style={{ transform: `scaleX(${fraction()})` }} />
      </div>
      <div class="index-sub">
        <span class="index-phase">{ev()?.label || "Getting started"}</span>
        <Show when={counter()}>
          <span class="index-count">{counter()}</span>
        </Show>
        <Show when={eta()}>
          <span class="index-eta">{eta()}</span>
        </Show>
        <span class="grow" />
        <button class="link-btn" aria-expanded={details()} onClick={() => setDetails(!details())}>
          {details() ? "Hide details" : "Show details"}
        </button>
      </div>
      <Show when={details()}>
        <ol class="phases">
          <For each={PHASES}>
            {(p) => {
              const ph = () => props.job.phases[p.key];
              const st = () => ph()?.state || "pending";
              return (
                <li class={`phase ${st()}`}>
                  <span class="phase-icon" aria-hidden="true">
                    {st() === "running" ? <Spinner /> : st() === "done" ? "✓" : st() === "skipped" ? "–" : st() === "failed" ? "✗" : "·"}
                  </span>
                  <span class="phase-label">{ph()?.label || p.label}</span>
                  <span class="phase-detail">{st() === "pending" ? "" : ph()?.detail || ""}</span>
                  <span class="sr-only">{st()}</span>
                </li>
              );
            }}
          </For>
        </ol>
      </Show>
    </section>
  );
}

// ------------------------------------------------------------------------------------------ ask progress
function AskProgress(props: { asking: Asking; job: JobView | null; now: number; onCancel: () => void }) {
  const ev = () => props.job?.ask || null;
  const steps = () => ev()?.steps || 4;
  const step = () => ev()?.step || 1;
  const label = () => {
    if (props.job?.cancelling) return "Stopping";
    if (ev()?.label) return ev()!.label;
    if (props.job?.state === "queued") return "Waiting for the engine";
    return "Understanding your question";
  };
  const elapsed = () => Math.max(0, Math.floor((props.now - props.asking.started) / 1000));
  return (
    <div class="ask-progress" role="status" aria-live="polite">
      <Spinner />
      <span class="ask-label">{label()}…</span>
      <span class="step-dots" aria-label={`Step ${step()} of ${steps()}`}>
        <For each={Array.from({ length: steps() }, (_, i) => i + 1)}>
          {(i) => <span class="step-dot" classList={{ on: i <= step() }} />}
        </For>
      </span>
      <span class="ask-elapsed">{elapsed()} s</span>
      <span class="grow" />
      <button class="btn subtle sm" onClick={props.onCancel} disabled={!props.asking.jobId || !!props.job?.cancelling}>
        Cancel
      </button>
    </div>
  );
}

// ------------------------------------------------------------------------------------------ recent questions
function HistoryMenu(props: { items: HistoryItem[]; onPick: (h: HistoryItem) => void }) {
  const [open, setOpen] = createSignal(false);
  let root: HTMLDivElement | undefined;
  const onDoc = (e: MouseEvent) => {
    if (root && !root.contains(e.target as Node)) setOpen(false);
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") setOpen(false);
  };
  document.addEventListener("mousedown", onDoc);
  document.addEventListener("keydown", onKey);
  onCleanup(() => {
    document.removeEventListener("mousedown", onDoc);
    document.removeEventListener("keydown", onKey);
  });
  return (
    <Show when={props.items.length}>
      <div class="menu" ref={root}>
        <button class="btn sm subtle" aria-haspopup="true" aria-expanded={open()} onClick={() => setOpen(!open())}>
          Recent questions <span class="caret" aria-hidden="true">▾</span>
        </button>
        <Show when={open()}>
          <ul class="menu-list" role="menu">
            <For each={props.items}>
              {(h) => (
                <li role="none">
                  <button
                    role="menuitem"
                    class="menu-item"
                    onClick={() => {
                      setOpen(false);
                      props.onPick(h);
                    }}
                  >
                    <span class="menu-q">{h.question}</span>
                    <span class="menu-when">{timeAgo(h.created)}</span>
                  </button>
                </li>
              )}
            </For>
          </ul>
        </Show>
      </div>
    </Show>
  );
}
