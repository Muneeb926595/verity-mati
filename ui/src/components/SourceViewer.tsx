import { createMemo, createSignal, For, onCleanup, onMount, Show } from "solid-js";
import { errorText, rpc } from "../api";
import { highlight } from "../highlight";
import type { SourceSlice, SourceTarget } from "../types";
import { Banner, Spinner } from "./common";

const MAX_LINES = 800; // never render more than this many lines at once

export interface PickedLine {
  file: string;
  line: number;
  note: string;
}

export default function SourceViewer(props: {
  path: string;
  target: SourceTarget;
  onClose: () => void;
  /** pick mode (target.pick set): the person chose the line that answers the question */
  onPicked?: (sel: PickedLine) => void;
  pickBusy?: string;
  pickError?: string | null;
}) {
  const [slice, setSlice] = createSignal<SourceSlice | null>(null);
  const [error, setError] = createSignal<string | null>(null);
  const [loading, setLoading] = createSignal(false);
  const picking = () => !!props.target.pick;
  // the file shown: the target's, or (picking) another one the person went to
  const [where, setWhere] = createSignal<{ file: string; line: number | null; end: number | null }>({
    file: props.target.file, line: props.target.line || null, end: props.target.end || null });
  const [picked, setPicked] = createSignal<number | null>(props.target.pick ? props.target.line || null : null);
  const [findText, setFindText] = createSignal("");
  const [found, setFound] = createSignal<Array<{ name: string; kind: string; file: string; line: number }>>([]);
  let findTimer: number | undefined;
  const [note, setNote] = createSignal("");
  let noteInput: HTMLInputElement | undefined;
  const pickedText = () => {
    const s = slice();
    const n = picked();
    if (!s || !n || n < s.start || n > s.end) return "";
    return s.lines[n - s.start].trim();
  };
  const usePicked = () => {
    const n = picked();
    if (!n || !props.onPicked || props.pickBusy) return;
    props.onPicked({ file: where().file, line: n, note: note().trim() });
  };
  const movePick = (d: number) => {
    const s = slice();
    if (!s) return;
    const n = Math.min(s.end, Math.max(s.start, (picked() || where().line || s.start) + d));
    setPicked(n);
    requestAnimationFrame(() => scroller?.querySelector<HTMLElement>(".row.picked")?.scrollIntoView({ block: "nearest" }));
  };
  let scroller: HTMLDivElement | undefined;
  let panel: HTMLElement | undefined;
  let closeBtn: HTMLButtonElement | undefined;
  const line = () => where().line || 1;
  const end = () => Math.max(line(), where().end || line());

  /** Picking: show another file (a function found by name, or a file the answer mentions). */
  function goTo(file: string, ln: number | null) {
    setWhere({ file, line: ln, end: null });
    setPicked(ln);
    setFound([]);
    setFindText("");
    setSlice(null);
    load();
  }

  function find(q: string) {
    setFindText(q);
    clearTimeout(findTimer);
    if (q.trim().length < 2) {
      setFound([]);
      return;
    }
    findTimer = window.setTimeout(async () => {
      try {
        const hits = await rpc<Array<{ name: string; kind: string; file: string; line: number }>>("search", { path: props.path, query: q.trim(), k: 8 });
        if (findText() === q) setFound(hits.filter((h) => h.file && h.line));
      } catch {
        setFound([]);
      }
    }, 220);
  }

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const s = await rpc<SourceSlice>("source.get", { path: props.path, file: where().file, around: line() });
      setSlice(s);
      requestAnimationFrame(() => {
        const el = scroller?.querySelector<HTMLElement>(".row.target");
        el?.scrollIntoView({ block: "center" });
      });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  }

  async function more(direction: "up" | "down") {
    const s = slice();
    if (!s || loading()) return;
    const start = direction === "up" ? Math.max(1, s.start - 80) : s.end + 1;
    const stop = direction === "up" ? s.start - 1 : Math.min(s.total, s.end + 80);
    if (stop < start) return;
    setLoading(true);
    const keep = scroller ? scroller.scrollHeight - scroller.scrollTop : 0;
    try {
      const add = await rpc<SourceSlice>("source.get", { path: props.path, file: s.file, start, end: stop });
      let lines = direction === "up" ? [...add.lines, ...s.lines] : [...s.lines, ...add.lines];
      let nStart = direction === "up" ? add.start : s.start;
      if (lines.length > MAX_LINES) {
        // drop lines from the far side so the page stays light
        const drop = lines.length - MAX_LINES;
        if (direction === "up") lines = lines.slice(0, MAX_LINES);
        else {
          lines = lines.slice(drop);
          nStart += drop;
        }
      }
      setSlice({ ...s, start: nStart, end: nStart + lines.length - 1, lines, stale: s.stale || add.stale });
      if (direction === "up" && scroller) {
        const el = scroller;
        requestAnimationFrame(() => (el.scrollTop = el.scrollHeight - keep));
      }
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  }

  const rows = createMemo(() => {
    const s = slice();
    if (!s) return [];
    const toks = highlight(s.lines, s.lang);
    return s.lines.map((_, i) => ({ n: s.start + i, toks: toks[i] }));
  });

  onMount(() => {
    load();
    const prev = document.activeElement as HTMLElement | null;
    closeBtn?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        props.onClose();
        return;
      }
      if (!picking() || e.target === noteInput) {
        if (picking() && e.key === "Enter" && e.target === noteInput) {
          e.preventDefault();
          usePicked();
        }
        return;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        movePick(e.key === "ArrowDown" ? 1 : -1);
      } else if (e.key === "Enter" && picked()) {
        e.preventDefault();
        usePicked();
      }
    };
    document.addEventListener("keydown", onKey);
    onCleanup(() => {
      document.removeEventListener("keydown", onKey);
      prev?.focus?.({ preventScroll: true });
    });
  });

  return (
    <>
      <div class="panel-backdrop" onClick={() => props.onClose()} />
      <aside class="source-panel" classList={{ picking: picking() }} ref={panel} role="dialog"
        aria-label={picking() ? `Pick the line that answers your question, in ${where().file}` : `Source code: ${where().file}`}>
        <header class="src-head">
          <div class="src-title">
            <span class="src-file" title={where().file}>
              {where().file}
            </span>
            <span class="src-line">
              {end() > line() ? `lines ${line()}–${end()}` : `line ${line()}`}
              <Show when={slice()}> of {slice()!.total}</Show>
            </span>
          </div>
          <Show when={loading()}>
            <Spinner />
          </Show>
          <Show when={!picking()} fallback={<span class="src-ro pick" title="Click the line that answers the question">pick a line</span>}>
            <span class="src-ro" title="Verity never changes your files">read-only</span>
          </Show>
          <button class="icon-btn" ref={closeBtn} aria-label="Close source viewer" title="Close (Esc)" onClick={() => props.onClose()}>
            ×
          </button>
        </header>
        <Show when={picking()}>
          <div class="src-pickhead">
            <strong>Which line answers your question?</strong>
            <span class="muted"> “{props.target.pick!.question}”</span>
            <div class="muted small">Click the line (or use ↑ ↓), then press Use this line. Usually it is the check that decides, or the line that handles the case.</div>
            <div class="pick-elsewhere">
              <span class="muted small">Look elsewhere:</span>
              <For each={(props.target.pick!.files || []).filter((f) => f !== where().file).slice(0, 4)}>
                {(f) => (
                  <button class="chip sm" onClick={() => goTo(f, null)} title={`Show ${f}`}>
                    {f.split("/").pop()}
                  </button>
                )}
              </For>
              <span class="pick-find">
                <input type="search" placeholder="Go to a function…" aria-label="Go to a function by name" value={findText()}
                  onInput={(e) => find(e.currentTarget.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && found().length) {
                      e.preventDefault();
                      goTo(found()[0].file, found()[0].line);
                    }
                  }} />
                <Show when={found().length}>
                  <ul class="pick-results" role="listbox" aria-label="Functions found">
                    <For each={found()}>
                      {(h) => (
                        <li role="option" aria-selected={false}>
                          <button onClick={() => goTo(h.file, h.line)}>
                            <span class="pr-name">{h.name}</span> <span class="muted small">{h.kind} · {h.file}:{h.line}</span>
                          </button>
                        </li>
                      )}
                    </For>
                  </ul>
                </Show>
              </span>
            </div>
          </div>
        </Show>
        <Show when={slice()?.stale}>
          <div class="src-banner">
            This file has changed since Verity last read the project, so the highlighted line may have moved. Press Update at the
            top to refresh.
          </div>
        </Show>
        <Show when={error()}>
          <div class="src-error">
            <Banner kind="error">{error()}</Banner>
          </div>
        </Show>
        <div class="src-body" ref={scroller}>
          <Show when={slice() && slice()!.start > 1}>
            <button class="src-more" onClick={() => more("up")} disabled={loading()}>
              Show earlier lines
            </button>
          </Show>
          <div class="code" role={picking() ? "listbox" : "list"} aria-label={picking() ? "Lines of code" : undefined}>
            <For each={rows()}>
              {(r) => (
                <div
                  class="row"
                  role={picking() ? "option" : "listitem"}
                  aria-selected={picking() ? r.n === picked() : undefined}
                  classList={{ hl: !picking() && r.n >= line() && r.n <= end(), target: r.n === line(), picked: picking() && r.n === picked() }}
                  onClick={() => picking() && setPicked(r.n)}
                >
                  <span class="ln" aria-hidden="true">
                    {r.n}
                  </span>
                  <span class="tx">
                    <For each={r.toks}>{(t) => (t.k ? <span class={`t-${t.k}`}>{t.v}</span> : t.v)}</For>
                    {r.toks.length ? "" : " "}
                  </span>
                </div>
              )}
            </For>
          </div>
          <Show when={slice() && slice()!.end < slice()!.total}>
            <button class="src-more" onClick={() => more("down")} disabled={loading()}>
              Show later lines
            </button>
          </Show>
          <Show when={!slice() && loading()}>
            <div class="muted pad">
              <Spinner /> Loading…
            </div>
          </Show>
        </div>
        <Show when={picking()}>
          <div class="src-pickbar">
            <div class="pick-sel">
              <Show when={picked()} fallback={<span class="muted">No line picked yet.</span>}>
                <span class="pick-ln">Line {picked()}</span> <code class="ic">{pickedText().slice(0, 140) || "(empty line)"}</code>
              </Show>
            </div>
            <div class="pick-row">
              <input
                ref={noteInput}
                class="pick-note"
                type="text"
                maxLength={500}
                placeholder="Why this line? (optional, shown with later answers)"
                aria-label="Note for this correction (optional)"
                value={note()}
                onInput={(e) => setNote(e.currentTarget.value)}
              />
              <button class="btn sm" onClick={() => props.onClose()}>Cancel</button>
              <button class="btn sm primary" disabled={!picked() || !!props.pickBusy || !pickedText()} onClick={usePicked}>
                <Show when={props.pickBusy} fallback="Use this line">
                  <Spinner /> {props.pickBusy}
                </Show>
              </button>
            </div>
            <Show when={props.pickError}>
              <Banner kind="error">{props.pickError}</Banner>
            </Show>
          </div>
        </Show>
      </aside>
    </>
  );
}
